"""服务层测试：幂等去重、冲突决议、授权到期、并发签发、撤回与回滚、依赖约束、重启恢复。"""
import tempfile
import threading
import unittest
from pathlib import Path

from service_09252_004.errors import (
    ConflictError,
    ConstraintViolation,
    NotFoundError,
    ValidationError,
)
from service_09252_004.ports import ManualClock, SequentialIds
from service_09252_004.service import CourseHubService

T0 = 1_700_000_000.0


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "coursehub.db")
        self.clock = ManualClock(T0)
        self.svc = CourseHubService(self.db_path, clock=self.clock, ids=SequentialIds())
        self.addCleanup(self.svc.close)

    # ---------------------------------------------------------- 装配辅助

    def add_component(self, title="课程包", author="教研组", region="CN", payload=None):
        return self.svc.upload_component(
            title=title,
            author=author,
            origin_region=region,
            payload=payload if payload is not None else {"lessons": ["L1"]},
        )["component"]

    def add_variant(self, component_id, language="zh", region="EU", payload=None):
        return self.svc.upload_variant(
            component_id=component_id,
            language=language,
            region=region,
            subtitles=[language],
            cases=[],
            payload=payload if payload is not None else {"variant": language},
        )["variant"]

    def add_license(self, component_id, regions=("EU",), standards=(), valid_until=None):
        return self.svc.register_license(
            component_id=component_id,
            licensor="版权方",
            regions=list(regions),
            standards=list(standards),
            valid_until=valid_until,
        )["license"]

    def add_candidate(self, region, *variant_ids):
        return self.svc.create_candidate(region=region, variant_ids=list(variant_ids))[
            "candidate"
        ]

    def approve(self, candidate_id, reviewer="教研员A"):
        return self.svc.submit_review(
            candidate_id=candidate_id, reviewer=reviewer, decision="approve"
        )

    def signable_candidate(self, region="EU", standards=()):
        """组件 + 变体 + 授权 + 候选 + 一条通过评审，可直接签发。"""
        comp = self.add_component()
        var = self.add_variant(comp["component_id"], region=region)
        self.add_license(comp["component_id"], regions=(region,), standards=standards)
        cand = self.add_candidate(region, var["variant_id"])
        self.approve(cand["candidate_id"])
        return comp, var, cand


class UploadDedupTests(ServiceTestBase):
    def test_component_upload_is_idempotent(self) -> None:
        first = self.svc.upload_component(
            title="物理", author="甲校", origin_region="CN", payload={"grade": 9}
        )
        second = self.svc.upload_component(
            title="物理", author="甲校", origin_region="CN", payload={"grade": 9}
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(
            first["component"]["component_id"], second["component"]["component_id"]
        )
        rows = self.svc.storage.all("SELECT * FROM components")
        self.assertEqual(len(rows), 1)

    def test_different_content_creates_new_component(self) -> None:
        self.add_component(payload={"v": 1})
        self.add_component(payload={"v": 2})
        rows = self.svc.storage.all("SELECT * FROM components")
        self.assertEqual(len(rows), 2)

    def test_variant_upload_is_idempotent(self) -> None:
        comp = self.add_component()
        kwargs = dict(
            component_id=comp["component_id"],
            language="zh",
            region="EU",
            subtitles=["zh", "en"],
            cases=["case-1"],
            payload={"subtitles": "v1"},
        )
        first = self.svc.upload_variant(**kwargs)
        second = self.svc.upload_variant(**kwargs)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(
            first["variant"]["variant_id"], second["variant"]["variant_id"]
        )

    def test_upload_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.upload_component(
                title="  ", author="a", origin_region="CN", payload={}
            )
        with self.assertRaises(ValidationError):
            self.svc.upload_component(
                title="t", author="a", origin_region="CN", payload=["not-a-dict"]
            )
        with self.assertRaises(NotFoundError):
            self.svc.upload_variant(
                component_id="cmp_missing", language="zh", region="EU"
            )


class MergeConflictTests(ServiceTestBase):
    def test_same_component_variants_conflict(self) -> None:
        comp = self.add_component()
        v_zh = self.add_variant(comp["component_id"], language="zh")
        v_en = self.add_variant(comp["component_id"], language="en")
        with self.assertRaises(ConflictError) as ctx:
            self.add_candidate("EU", v_zh["variant_id"], v_en["variant_id"])
        self.assertIn(comp["component_id"], ctx.exception.details["component_ids"])

    def test_candidate_merge_is_idempotent_and_order_insensitive(self) -> None:
        comp_a = self.add_component(title="A", payload={"c": "a"})
        comp_b = self.add_component(title="B", payload={"c": "b"})
        v_a = self.add_variant(comp_a["component_id"])
        v_b = self.add_variant(comp_b["component_id"])
        first = self.svc.create_candidate(
            region="EU", variant_ids=[v_a["variant_id"], v_b["variant_id"]]
        )
        second = self.svc.create_candidate(
            region="EU", variant_ids=[v_b["variant_id"], v_a["variant_id"]]
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(
            first["candidate"]["candidate_id"], second["candidate"]["candidate_id"]
        )
        # 原稿不被合并过程修改
        self.assertEqual(
            self.svc.get_component(comp_a["component_id"])["payload"], {"c": "a"}
        )

    def test_candidate_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.create_candidate(region="EU", variant_ids=[])
        with self.assertRaises(NotFoundError):
            self.svc.create_candidate(region="EU", variant_ids=["var_missing"])


class ReviewConflictTests(ServiceTestBase):
    def test_reject_blocks_until_reviewer_supersedes(self) -> None:
        self.svc.set_region_policy("EU", required_approvals=2)
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        self.add_license(comp["component_id"])
        cand = self.add_candidate("EU", var["variant_id"])

        self.approve(cand["candidate_id"], reviewer="教研员A")
        self.svc.submit_review(
            candidate_id=cand["candidate_id"],
            reviewer="版权审核员B",
            decision="reject",
            comment="字幕未署名",
        )
        # 冲突未决议：拒绝意见生效，且通过人数不足
        check = self.svc.check_candidate(cand["candidate_id"])
        self.assertFalse(check["signable"])
        types = {v["type"] for v in check["violations"]}
        self.assertIn("review_rejected", types)
        self.assertIn("reviews_insufficient", types)
        with self.assertRaises(ConstraintViolation):
            self.svc.sign_release(candidate_id=cand["candidate_id"])

        # 冲突决议：同一评审人改判为通过，最新意见生效
        self.svc.submit_review(
            candidate_id=cand["candidate_id"],
            reviewer="版权审核员B",
            decision="approve",
            comment="已补署名",
        )
        result = self.svc.sign_release(candidate_id=cand["candidate_id"])
        self.assertTrue(result["created"])
        self.assertEqual(result["release"]["status"], "published")

    def test_parallel_reviews_from_independent_reviewers(self) -> None:
        self.svc.set_region_policy("EU", required_approvals=3)
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        self.add_license(comp["component_id"])
        cand = self.add_candidate("EU", var["variant_id"])

        barrier = threading.Barrier(3)
        errors = []

        def reviewer(name):
            barrier.wait()
            try:
                self.approve(cand["candidate_id"], reviewer=name)
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

        threads = [
            threading.Thread(target=reviewer, args=(f"评审{i}",)) for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        result = self.svc.sign_release(candidate_id=cand["candidate_id"])
        self.assertTrue(result["created"])

    def test_identical_review_is_idempotent(self) -> None:
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        cand = self.add_candidate("EU", var["variant_id"])
        first = self.approve(cand["candidate_id"])
        second = self.approve(cand["candidate_id"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        rows = self.svc.storage.all("SELECT * FROM reviews")
        self.assertEqual(len(rows), 1)


class LicenseExpiryTests(ServiceTestBase):
    def test_expired_license_blocks_signing_and_flags_published(self) -> None:
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        self.add_license(comp["component_id"], valid_until=T0 + 100)
        cand = self.add_candidate("EU", var["variant_id"])
        self.approve(cand["candidate_id"])
        signed = self.svc.sign_release(candidate_id=cand["candidate_id"])
        self.assertTrue(signed["created"])
        release_id = signed["release"]["release_id"]

        # 时钟越过授权有效期
        self.clock.advance(200)

        # 未来签发被阻断，违例指明授权已过期
        var2 = self.add_variant(comp["component_id"], payload={"variant": "zh-v2"})
        cand2 = self.add_candidate("EU", var2["variant_id"])
        self.approve(cand2["candidate_id"])
        with self.assertRaises(ConstraintViolation) as ctx:
            self.svc.sign_release(candidate_id=cand2["candidate_id"])
        violations = ctx.exception.violations
        self.assertEqual(violations[0]["type"], "license_missing")
        self.assertTrue(
            any(c.startswith("expired:") for c in violations[0]["causes"]),
            msg=str(violations),
        )

        # 已发布版本被标出风险，但历史保留
        release = self.svc.get_release(release_id)
        self.assertEqual(release["status"], "at_risk")
        self.assertTrue(release["risk_reason"])
        self.assertEqual([e["event"] for e in release["events"]], ["signed", "at_risk"])

    def test_not_yet_valid_license_blocks_signing(self) -> None:
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        self.svc.register_license(
            component_id=comp["component_id"],
            licensor="版权方",
            regions=["EU"],
            valid_from=T0 + 1000,
            valid_until=T0 + 2000,
        )
        cand = self.add_candidate("EU", var["variant_id"])
        self.approve(cand["candidate_id"])
        with self.assertRaises(ConstraintViolation) as ctx:
            self.svc.sign_release(candidate_id=cand["candidate_id"])
        causes = ctx.exception.violations[0]["causes"]
        self.assertTrue(any(c.startswith("not_yet_valid:") for c in causes))

    def test_invalid_validity_window_rejected(self) -> None:
        comp = self.add_component()
        with self.assertRaises(ValidationError):
            self.svc.register_license(
                component_id=comp["component_id"],
                licensor="版权方",
                regions=["EU"],
                valid_from=T0 + 100,
                valid_until=T0 + 50,
            )


class WithdrawalTests(ServiceTestBase):
    def test_withdrawal_blocks_future_and_flags_published(self) -> None:
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        lic_a = self.add_license(comp["component_id"])
        lic_b = self.svc.register_license(
            component_id=comp["component_id"], licensor="版权方乙", regions=["EU"]
        )["license"]
        cand = self.add_candidate("EU", var["variant_id"])
        self.approve(cand["candidate_id"])
        release_id = self.svc.sign_release(candidate_id=cand["candidate_id"])[
            "release"
        ]["release_id"]

        # 撤回第一份授权：仍有 lic_b 覆盖，已发布版本不受影响
        result = self.svc.withdraw_license(lic_a["license_id"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["at_risk_release_ids"], [])
        self.assertEqual(self.svc.get_release(release_id)["status"], "published")

        # 撤回第二份授权：已发布版本标出风险，未来签发被阻断
        result = self.svc.withdraw_license(lic_b["license_id"])
        self.assertEqual(result["at_risk_release_ids"], [release_id])
        release = self.svc.get_release(release_id)
        self.assertEqual(release["status"], "at_risk")
        self.assertIn("license_missing", str(release["risk_reason"]))

        var2 = self.add_variant(comp["component_id"], payload={"variant": "zh-v2"})
        cand2 = self.add_candidate("EU", var2["variant_id"])
        self.approve(cand2["candidate_id"])
        with self.assertRaises(ConstraintViolation) as ctx:
            self.svc.sign_release(candidate_id=cand2["candidate_id"])
        causes = ctx.exception.violations[0]["causes"]
        self.assertTrue(any(c.startswith("withdrawn:") for c in causes))

    def test_withdrawal_is_idempotent_and_preserves_history(self) -> None:
        comp, var, cand = self.signable_candidate()
        lic = self.svc.list_licenses(comp["component_id"])[0]
        release_id = self.svc.sign_release(candidate_id=cand["candidate_id"])[
            "release"
        ]["release_id"]

        first = self.svc.withdraw_license(lic["license_id"])
        second = self.svc.withdraw_license(lic["license_id"])
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])

        # 授权行仍在，只是状态为 withdrawn（不静默删除）
        license_row = self.svc.get_license(lic["license_id"])
        self.assertEqual(license_row["status"], "withdrawn")
        self.assertIsNotNone(license_row["withdrawn_at"])
        # 发布版本与事件历史完整保留
        release = self.svc.get_release(release_id)
        self.assertEqual(release["status"], "at_risk")
        self.assertEqual([e["event"] for e in release["events"]], ["signed", "at_risk"])
        self.assertEqual(len(self.svc.list_releases()), 1)

    def test_license_covering_other_region_does_not_satisfy(self) -> None:
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        self.add_license(comp["component_id"], regions=("APAC",))
        cand = self.add_candidate("EU", var["variant_id"])
        self.approve(cand["candidate_id"])
        with self.assertRaises(ConstraintViolation) as ctx:
            self.svc.sign_release(candidate_id=cand["candidate_id"])
        causes = ctx.exception.violations[0]["causes"]
        self.assertTrue(any(c.startswith("region_not_covered:") for c in causes))


class RollbackTests(ServiceTestBase):
    def test_rollback_keeps_history_and_allows_resign(self) -> None:
        _, _, cand = self.signable_candidate()
        release_id = self.svc.sign_release(candidate_id=cand["candidate_id"])[
            "release"
        ]["release_id"]

        rolled = self.svc.rollback_release(release_id, reason="版权方要求下架")
        self.assertTrue(rolled["changed"])
        self.assertEqual(rolled["release"]["status"], "rolled_back")

        again = self.svc.rollback_release(release_id)
        self.assertFalse(again["changed"])

        release = self.svc.get_release(release_id)
        self.assertEqual(
            [e["event"] for e in release["events"]], ["signed", "rolled_back"]
        )
        self.assertEqual(release["events"][1]["reason"], "版权方要求下架")

        # 回滚后可重新签发为新版本，旧版本历史保留
        resigned = self.svc.sign_release(candidate_id=cand["candidate_id"])
        self.assertTrue(resigned["created"])
        self.assertNotEqual(resigned["release"]["release_id"], release_id)
        self.assertEqual(len(self.svc.list_releases()), 2)
        self.assertEqual(len(self.svc.list_releases(status="rolled_back")), 1)
        self.assertEqual(len(self.svc.list_releases(status="published")), 1)

    def test_rollback_unknown_release(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.rollback_release("rel_missing")


class DependencyTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.lib = self.add_component(title="字幕库", payload={"c": "lib"})
        self.course = self.add_component(title="主课程", payload={"c": "course"})

    def test_dependency_must_also_be_licensed(self) -> None:
        self.svc.add_dependency(
            component_id=self.course["component_id"],
            depends_on_id=self.lib["component_id"],
        )
        var = self.add_variant(self.course["component_id"])
        self.add_license(self.course["component_id"])  # 只授权主课程
        cand = self.add_candidate("EU", var["variant_id"])
        self.approve(cand["candidate_id"])
        with self.assertRaises(ConstraintViolation) as ctx:
            self.svc.sign_release(candidate_id=cand["candidate_id"])
        missing = [
            v["component_id"]
            for v in ctx.exception.violations
            if v["type"] == "license_missing"
        ]
        self.assertEqual(missing, [self.lib["component_id"]])

        self.add_license(self.lib["component_id"])
        result = self.svc.sign_release(candidate_id=cand["candidate_id"])
        self.assertTrue(result["created"])

    def test_dependency_cycle_rejected(self) -> None:
        self.svc.add_dependency(
            component_id=self.course["component_id"],
            depends_on_id=self.lib["component_id"],
        )
        with self.assertRaises(ConflictError):
            self.svc.add_dependency(
                component_id=self.lib["component_id"],
                depends_on_id=self.course["component_id"],
            )
        with self.assertRaises(ValidationError):
            self.svc.add_dependency(
                component_id=self.lib["component_id"],
                depends_on_id=self.lib["component_id"],
            )

    def test_dependency_fingerprint_pin(self) -> None:
        self.svc.add_dependency(
            component_id=self.course["component_id"],
            depends_on_id=self.lib["component_id"],
            required_fingerprint="sha256:stale",
        )
        var = self.add_variant(self.course["component_id"])
        self.add_license(self.course["component_id"])
        self.add_license(self.lib["component_id"])
        cand = self.add_candidate("EU", var["variant_id"])
        self.approve(cand["candidate_id"])
        with self.assertRaises(ConstraintViolation) as ctx:
            self.svc.sign_release(candidate_id=cand["candidate_id"])
        types = {v["type"] for v in ctx.exception.violations}
        self.assertIn("dependency_fingerprint_mismatch", types)

        # 钉扎更新为当前指纹后放行
        self.svc.add_dependency(
            component_id=self.course["component_id"],
            depends_on_id=self.lib["component_id"],
            required_fingerprint=self.lib["fingerprint"],
        )
        result = self.svc.sign_release(candidate_id=cand["candidate_id"])
        self.assertTrue(result["created"])


class RegionPolicyTests(ServiceTestBase):
    def test_required_standards_gate_signing(self) -> None:
        self.svc.set_region_policy("EU", required_standards=["STD-无障碍-1"])
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        self.add_license(comp["component_id"])  # 未声明任何标准
        cand = self.add_candidate("EU", var["variant_id"])
        self.approve(cand["candidate_id"])
        with self.assertRaises(ConstraintViolation) as ctx:
            self.svc.sign_release(candidate_id=cand["candidate_id"])
        std = [v for v in ctx.exception.violations if v["type"] == "standards_missing"]
        self.assertEqual(std[0]["missing"], ["STD-无障碍-1"])

        self.add_license(comp["component_id"], standards=("STD-无障碍-1",))
        result = self.svc.sign_release(candidate_id=cand["candidate_id"])
        self.assertTrue(result["created"])

    def test_default_policy_requires_one_approval(self) -> None:
        comp = self.add_component()
        var = self.add_variant(comp["component_id"])
        self.add_license(comp["component_id"])
        cand = self.add_candidate("EU", var["variant_id"])
        with self.assertRaises(ConstraintViolation) as ctx:
            self.svc.sign_release(candidate_id=cand["candidate_id"])
        types = {v["type"] for v in ctx.exception.violations}
        self.assertIn("reviews_insufficient", types)


class ConcurrencyTests(ServiceTestBase):
    def test_concurrent_signing_single_winner(self) -> None:
        _, _, cand = self.signable_candidate()
        barrier = threading.Barrier(8)
        results, errors = [], []

        def worker():
            barrier.wait()
            try:
                results.append(self.svc.sign_release(candidate_id=cand["candidate_id"]))
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        release_ids = {r["release"]["release_id"] for r in results}
        self.assertEqual(len(release_ids), 1, "并发签发必须收敛到同一版本")
        self.assertEqual(sum(1 for r in results if r["created"]), 1)
        rows = self.svc.storage.all(
            "SELECT * FROM releases WHERE candidate_id=?", (cand["candidate_id"],)
        )
        self.assertEqual(len(rows), 1)

    def test_concurrent_signing_distinct_candidates_all_succeed(self) -> None:
        comp = self.add_component()
        self.add_license(comp["component_id"])
        candidates = []
        for i in range(4):
            var = self.add_variant(comp["component_id"], payload={"variant": f"v{i}"})
            cand = self.add_candidate("EU", var["variant_id"])
            self.approve(cand["candidate_id"])
            candidates.append(cand["candidate_id"])

        barrier = threading.Barrier(4)
        errors = []

        def worker(cid):
            barrier.wait()
            try:
                self.svc.sign_release(candidate_id=cid)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(c,)) for c in candidates]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.svc.list_releases(status="published")), 4)

    def test_concurrent_upload_deduplicates(self) -> None:
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(
                self.svc.upload_component(
                    title="公共课", author="联合教研", origin_region="CN",
                    payload={"same": "content"},
                )
            )

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = {r["component"]["component_id"] for r in results}
        self.assertEqual(len(ids), 1)
        self.assertEqual(sum(1 for r in results if r["created"]), 1)
        self.assertEqual(len(self.svc.storage.all("SELECT * FROM components")), 1)


class RestartRecoveryTests(ServiceTestBase):
    def test_state_survives_restart(self) -> None:
        # 第一份发布：保持 published
        _, _, cand_ok = self.signable_candidate()
        rel_ok = self.svc.sign_release(candidate_id=cand_ok["candidate_id"])["release"][
            "release_id"
        ]
        # 第二份发布：随后撤回授权 → at_risk
        comp2 = self.add_component(title="第二门课", payload={"c": "second"})
        var2 = self.add_variant(comp2["component_id"])
        lic2 = self.add_license(comp2["component_id"])
        cand2 = self.add_candidate("EU", var2["variant_id"])
        self.approve(cand2["candidate_id"])
        rel_risk = self.svc.sign_release(candidate_id=cand2["candidate_id"])[
            "release"
        ]["release_id"]
        self.svc.withdraw_license(lic2["license_id"])
        self.assertEqual(self.svc.get_release(rel_risk)["status"], "at_risk")

        # 模拟重启：关闭后用同一数据库文件重新打开
        self.svc.close()
        svc2 = CourseHubService(self.db_path, clock=self.clock, ids=SequentialIds())
        self.addCleanup(svc2.close)

        self.assertEqual(svc2.get_release(rel_ok)["status"], "published")
        risk = svc2.get_release(rel_risk)
        self.assertEqual(risk["status"], "at_risk")
        self.assertEqual([e["event"] for e in risk["events"]], ["signed", "at_risk"])
        self.assertEqual(svc2.get_license(lic2["license_id"])["status"], "withdrawn")
        self.assertEqual(
            svc2.get_component(comp2["component_id"])["title"], "第二门课"
        )
        # 重启后约束仍然生效：被撤回授权的组件不能签发
        var_new = svc2.upload_variant(
            component_id=comp2["component_id"],
            language="zh",
            region="EU",
            subtitles=["zh"],
            cases=[],
            payload={"variant": "after-restart"},
        )["variant"]
        cand_new = svc2.create_candidate(
            region="EU", variant_ids=[var_new["variant_id"]]
        )["candidate"]
        svc2.submit_review(
            candidate_id=cand_new["candidate_id"],
            reviewer="教研员A",
            decision="approve",
        )
        with self.assertRaises(ConstraintViolation):
            svc2.sign_release(candidate_id=cand_new["candidate_id"])
        # 幂等性在重启后同样成立：重复上传返回既有组件
        again = svc2.upload_component(
            title="第二门课", author="教研组", origin_region="CN",
            payload={"c": "second"},
        )
        self.assertFalse(again["created"])
        self.assertEqual(again["component"]["component_id"], comp2["component_id"])


if __name__ == "__main__":
    unittest.main()
