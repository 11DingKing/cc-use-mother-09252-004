"""应用服务测试：冲突决议、授权到期、并发签发、撤回风险与重启恢复。"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest

from service_09252_004.domain import (
    LICENSE_WITHDRAWN,
    RELEASE_ACTIVE,
    RELEASE_ROLLED_BACK,
    RELEASE_SUPERSEDED,
    RISK_AT_RISK,
)
from service_09252_004.errors import ConflictError, NotFoundError, ValidationError
from service_09252_004.ports import ManualClock, SequentialIds
from service_09252_004.service import ReleaseService

REGION = "EU"
STANDARD = "STD-A"


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "releases.db")
        self.clock = ManualClock()
        self.service = ReleaseService(
            self.db_path, clock=self.clock, id_gen=SequentialIds()
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    # ---- 测试辅助 ----
    def _component_with_variant(
        self, name="字幕", kind="subtitle", language="zh-CN",
        content=None, standards=(STANDARD,),
    ):
        comp = self.service.create_component(name, kind, author="teacher-a")
        variant, _ = self.service.upload_variant(
            comp.id, language, content if content is not None else {"text": "你好"},
            standards=list(standards), created_by="teacher-a",
        )
        return comp, variant

    def _publishable_candidate(self, course="course-1", region=REGION,
                               standard=STANDARD, reviewers=("alice", "bob")):
        comp, variant = self._component_with_variant()
        lic = self.service.grant_license(
            comp.id, licensor="teacher-a", regions=[region], standards=[standard]
        )
        cand = self.service.create_candidate(
            course, region, standard, {comp.id: variant.id}, title="联合课程"
        )
        for reviewer in reviewers:
            self.service.submit_review(cand.id, reviewer, "approve")
        return comp, variant, lic, cand


class VariantUploadTests(ServiceTestCase):
    def test_upload_dedup_by_fingerprint(self) -> None:
        comp = self.service.create_component("字幕", "subtitle")
        payload = {"language": "zh-CN", "content": {"text": "你好"},
                   "standards": [STANDARD]}
        first, created1 = self.service.upload_variant(comp.id, **payload)
        second, created2 = self.service.upload_variant(comp.id, **payload)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(self.service.list_variants(comp.id)), 1)

    def test_same_content_different_language_not_deduplicated(self) -> None:
        comp = self.service.create_component("字幕", "subtitle")
        zh, _ = self.service.upload_variant(comp.id, "zh-CN", {"text": "你好"})
        en, _ = self.service.upload_variant(comp.id, "en", {"text": "你好"})
        self.assertNotEqual(zh.id, en.id)
        self.assertEqual(len(self.service.list_variants(comp.id)), 2)


class ConflictResolutionTests(ServiceTestCase):
    def test_variant_conflict_resolved_by_explicit_selection(self) -> None:
        """两位教师上传同一语言的不同字幕：都保留，候选显式选择其一。"""
        comp = self.service.create_component("字幕", "subtitle")
        var_a, _ = self.service.upload_variant(
            comp.id, "zh-CN", {"text": "版本A"}, standards=[STANDARD], created_by="teacher-a"
        )
        var_b, _ = self.service.upload_variant(
            comp.id, "zh-CN", {"text": "版本B"}, standards=[STANDARD], created_by="teacher-b"
        )
        # 平台识别冲突，且两个原稿都未被覆盖。
        conflicts = self.service.variant_conflicts(comp.id)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["language"], "zh-CN")
        self.assertEqual(
            {v["variant_id"] for v in conflicts[0]["variants"]}, {var_a.id, var_b.id}
        )
        self.assertEqual(self.service._get_variant(var_a.id).content, {"text": "版本A"})
        self.assertEqual(self.service._get_variant(var_b.id).content, {"text": "版本B"})

        self.service.grant_license(comp.id, regions=[REGION], standards=[STANDARD])
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: var_b.id}
        )
        self.service.submit_review(cand.id, "alice", "approve")
        self.service.submit_review(cand.id, "bob", "approve")
        release, created = self.service.publish(cand.id, issued_by="ops")
        self.assertTrue(created)
        # 发布快照精确引用被选中的变体，未选中者不受影响。
        selected = release["snapshot"]["selections"][0]
        self.assertEqual(selected["variant_id"], var_b.id)
        self.assertEqual(selected["variant_fingerprint"], var_b.fingerprint)
        self.assertEqual(len(self.service.list_variants(comp.id)), 2)

    def test_review_conflict_resolved_by_latest_decision(self) -> None:
        """评审意见冲突：同一评审人先拒后改，以最新意见为准。"""
        comp, variant = self._component_with_variant()
        self.service.grant_license(comp.id, regions=[REGION], standards=[STANDARD])
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}
        )
        self.service.submit_review(cand.id, "alice", "approve")
        self.service.submit_review(cand.id, "bob", "reject", comment="案例不适合目标地区")
        report = self.service.evaluate_candidate(cand.id)
        self.assertFalse(report["publishable"])
        self.assertIn("standing_rejections", {i["type"] for i in report["issues"]})

        # bob 复核后改为同意：拒绝被最新意见取代，候选可签发。
        self.service.submit_review(cand.id, "bob", "approve", comment="已更换案例")
        report = self.service.evaluate_candidate(cand.id)
        self.assertTrue(report["publishable"], report["issues"])
        self.assertEqual(len(self.service.list_reviews(cand.id)), 3)  # 历史意见全保留

    def test_parallel_reviews_all_recorded(self) -> None:
        """并行评审：多位评审人同时提交，意见无一丢失。"""
        comp, variant = self._component_with_variant()
        self.service.grant_license(comp.id, regions=[REGION], standards=[STANDARD])
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}, required_approvals=5
        )
        reviewers = [f"reviewer-{i}" for i in range(5)]
        barrier = threading.Barrier(len(reviewers))
        errors: list[BaseException] = []

        def work(name: str) -> None:
            try:
                barrier.wait(timeout=10)
                self.service.submit_review(cand.id, name, "approve")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(r,)) for r in reviewers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.service.list_reviews(cand.id)), 5)
        report = self.service.evaluate_candidate(cand.id)
        self.assertEqual(sorted(report["approvals"]), reviewers)
        self.assertTrue(report["publishable"])


class DependencyTests(ServiceTestCase):
    def test_candidate_requires_dependency_closure(self) -> None:
        video, video_var = self._component_with_variant("视频", "video", "en")
        sub, sub_var = self._component_with_variant("字幕", "subtitle", "zh-CN")
        self.service.add_dependency(sub.id, video.id, note="字幕依赖视频时间轴")

        with self.assertRaises(ValidationError) as ctx:
            self.service.create_candidate("course-1", REGION, STANDARD, {sub.id: sub_var.id})
        self.assertEqual(ctx.exception.details["missing_components"], [video.id])

        for comp in (video, sub):
            self.service.grant_license(comp.id, regions=[REGION], standards=[STANDARD])
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {sub.id: sub_var.id, video.id: video_var.id}
        )
        self.service.submit_review(cand.id, "alice", "approve")
        self.service.submit_review(cand.id, "bob", "approve")
        release, _ = self.service.publish(cand.id)
        self.assertEqual(len(release["snapshot"]["selections"]), 2)

    def test_dependency_cycle_rejected(self) -> None:
        a = self.service.create_component("A", "doc")
        b = self.service.create_component("B", "doc")
        self.service.add_dependency(a.id, b.id)
        with self.assertRaises(ValidationError):
            self.service.add_dependency(b.id, a.id)
        with self.assertRaises(ValidationError):
            self.service.add_dependency(a.id, a.id)


class ConstraintTests(ServiceTestCase):
    def test_standard_and_region_constraints(self) -> None:
        comp, variant = self._component_with_variant(standards=("STD-B",))
        self.service.grant_license(comp.id, regions=["US"], standards=[STANDARD])
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}
        )
        self.service.submit_review(cand.id, "alice", "approve")
        self.service.submit_review(cand.id, "bob", "approve")
        report = self.service.evaluate_candidate(cand.id)
        types = {i["type"] for i in report["issues"]}
        self.assertIn("standard_not_supported", types)  # 变体未声明目标标准
        self.assertIn("license_scope_uncovered", types)  # 授权不覆盖目标地区
        with self.assertRaises(ValidationError):
            self.service.publish(cand.id)

    def test_missing_license_blocks_publish(self) -> None:
        comp, variant = self._component_with_variant()
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}
        )
        self.service.submit_review(cand.id, "alice", "approve")
        self.service.submit_review(cand.id, "bob", "approve")
        report = self.service.evaluate_candidate(cand.id)
        self.assertIn("license_missing", {i["type"] for i in report["issues"]})
        with self.assertRaises(ValidationError):
            self.service.publish(cand.id)

    def test_insufficient_approvals_blocks_publish(self) -> None:
        comp, variant = self._component_with_variant()
        self.service.grant_license(comp.id, regions=[REGION], standards=[STANDARD])
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}
        )
        self.service.submit_review(cand.id, "alice", "approve")
        report = self.service.evaluate_candidate(cand.id)
        self.assertIn("insufficient_approvals", {i["type"] for i in report["issues"]})


class LicenseExpiryTests(ServiceTestCase):
    def test_expiry_blocks_future_publish_and_flags_release(self) -> None:
        """授权到期：阻断后续签发，已发布版本标记风险，历史保留。"""
        comp, variant = self._component_with_variant()
        lic = self.service.grant_license(
            comp.id, regions=[REGION], standards=[STANDARD],
            valid_until=self.clock.now + 100,
        )
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}
        )
        self.service.submit_review(cand.id, "alice", "approve")
        self.service.submit_review(cand.id, "bob", "approve")
        release, created = self.service.publish(cand.id)
        self.assertTrue(created)
        self.assertEqual(release["risk_status"], "ok")

        self.clock.advance(200)  # 授权到期
        view = self.service.get_release(release["id"])
        self.assertEqual(view["risk_status"], RISK_AT_RISK)
        self.assertEqual(view["risk_reasons"][0]["type"], "license_expired")
        self.assertEqual(view["risk_reasons"][0]["license_id"], lic.id)

        # 到期后新候选不得签发。
        cand2 = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}, title="再版"
        )
        self.service.submit_review(cand2.id, "alice", "approve")
        self.service.submit_review(cand2.id, "bob", "approve")
        report = self.service.evaluate_candidate(cand2.id)
        self.assertIn("license_expired", {i["type"] for i in report["issues"]})
        with self.assertRaises(ValidationError):
            self.service.publish(cand2.id)

    def test_not_yet_valid_license_blocks_publish(self) -> None:
        comp, variant = self._component_with_variant()
        self.service.grant_license(
            comp.id, regions=[REGION], standards=[STANDARD],
            valid_from=self.clock.now + 1000,
        )
        cand = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}
        )
        self.service.submit_review(cand.id, "alice", "approve")
        self.service.submit_review(cand.id, "bob", "approve")
        report = self.service.evaluate_candidate(cand.id)
        self.assertIn("license_not_yet_valid", {i["type"] for i in report["issues"]})


class WithdrawalTests(ServiceTestCase):
    def test_withdrawal_blocks_and_flags_without_deleting_history(self) -> None:
        comp, variant, lic, cand = self._publishable_candidate()
        release, _ = self.service.publish(cand.id)

        result = self.service.withdraw_license(lic.id, reason="版权方终止授权")
        self.assertEqual(result["license"].status, LICENSE_WITHDRAWN)
        self.assertEqual(result["affected_release_ids"], [release["id"]])

        # 已发布版本标记风险，但发布、快照与授权记录全部保留。
        view = self.service.get_release(release["id"])
        self.assertEqual(view["status"], RELEASE_ACTIVE)
        self.assertEqual(view["risk_status"], RISK_AT_RISK)
        self.assertEqual(view["risk_reasons"][0]["type"], "license_withdrawn")
        self.assertEqual(view["snapshot"]["selections"][0]["variant_id"], variant.id)
        self.assertEqual(self.service.get_license(lic.id).status, LICENSE_WITHDRAWN)

        # 撤回后任何包含该组件的新候选都不得签发。
        cand2 = self.service.create_candidate(
            "course-1", REGION, STANDARD, {comp.id: variant.id}, title="再版"
        )
        self.service.submit_review(cand2.id, "alice", "approve")
        self.service.submit_review(cand2.id, "bob", "approve")
        with self.assertRaises(ValidationError) as ctx:
            self.service.publish(cand2.id)
        types = {i["type"] for i in ctx.exception.details["issues"]}
        self.assertIn("license_withdrawn", types)

        # 重复撤回幂等，不重复标记。
        again = self.service.withdraw_license(lic.id, reason="重复操作")
        self.assertEqual(again["affected_release_ids"], [])
        self.assertEqual(len(self.service.get_release(release["id"])["risk_reasons"]), 1)


class PublishConcurrencyTests(ServiceTestCase):
    def _run_concurrently(self, fn, count=8):
        barrier = threading.Barrier(count)
        results: list = []
        errors: list[BaseException] = []

        def work():
            try:
                barrier.wait(timeout=10)
                results.append(fn())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        return results

    def test_concurrent_publish_same_candidate_is_idempotent(self) -> None:
        """并发签发同一候选：全部成功且得到同一个发布。"""
        _, _, _, cand = self._publishable_candidate()
        results = self._run_concurrently(lambda: self.service.publish(cand.id))
        release_ids = {rel["id"] for rel, _ in results}
        self.assertEqual(len(release_ids), 1)
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(len(self.service.list_releases()), 1)

    def test_concurrent_publish_keeps_single_active_release(self) -> None:
        """并发签发不同候选：不变式成立——同一课程/地区/标准恰有一个进行中发布。"""
        comp = self.service.create_component("字幕", "subtitle")
        self.service.grant_license(comp.id, regions=[REGION], standards=[STANDARD])
        candidates = []
        for i in range(8):
            variant, _ = self.service.upload_variant(
                comp.id, "zh-CN", {"text": f"版本{i}"}, standards=[STANDARD]
            )
            cand = self.service.create_candidate(
                "course-1", REGION, STANDARD, {comp.id: variant.id}
            )
            self.service.submit_review(cand.id, "alice", "approve")
            self.service.submit_review(cand.id, "bob", "approve")
            candidates.append(cand)

        results = self._run_concurrently(
            lambda: self.service.publish(_pop(candidates)), count=8
        )
        self.assertTrue(all(created for _, created in results))
        releases = self.service.list_releases()
        self.assertEqual(len(releases), 8)
        active = [r for r in releases if r["status"] == RELEASE_ACTIVE]
        superseded = [r for r in releases if r["status"] == RELEASE_SUPERSEDED]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(superseded), 7)


def _pop(stack: list):
    return stack.pop().id


class RollbackTests(ServiceTestCase):
    def test_rollback_keeps_history_and_allows_republish(self) -> None:
        _, _, _, cand = self._publishable_candidate()
        release, _ = self.service.publish(cand.id)

        rolled = self.service.rollback(release["id"], reason="运营撤回")
        self.assertEqual(rolled["status"], RELEASE_ROLLED_BACK)
        self.assertEqual(rolled["rollback_reason"], "运营撤回")
        self.assertIsNotNone(rolled["rolled_back_at"])

        with self.assertRaises(ConflictError):
            self.service.rollback(release["id"])  # 非进行中发布不可重复回滚

        # 回滚后可重新签发同一候选（产生新发布，旧记录保留）。
        reissued, created = self.service.publish(cand.id)
        self.assertTrue(created)
        self.assertNotEqual(reissued["id"], release["id"])
        releases = self.service.list_releases()
        self.assertEqual(len(releases), 2)
        self.assertEqual(
            {r["status"] for r in releases}, {RELEASE_ROLLED_BACK, RELEASE_ACTIVE}
        )

    def test_rollback_unknown_release(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.rollback("rel_missing")


class RestartRecoveryTests(ServiceTestCase):
    def test_state_survives_restart(self) -> None:
        """全部状态落盘：重启后发布、评审、撤回标记与约束检查继续生效。"""
        comp, variant, lic, cand = self._publishable_candidate()
        release, _ = self.service.publish(cand.id)
        blocked_comp, blocked_var = self._component_with_variant(name="案例", kind="case")
        blocked_lic = self.service.grant_license(
            blocked_comp.id, regions=[REGION], standards=[STANDARD]
        )
        self.service.withdraw_license(blocked_lic.id, reason="版权方终止授权")
        self.service.close()

        # 用同一数据文件重建服务（模拟进程重启）。
        self.service = ReleaseService(
            self.db_path, clock=self.clock, id_gen=SequentialIds()
        )
        view = self.service.get_release(release["id"])
        self.assertEqual(view["status"], RELEASE_ACTIVE)
        self.assertEqual(len(self.service.list_reviews(cand.id)), 2)

        # 重复签发幂等：返回既有发布而非新建。
        replay, created = self.service.publish(cand.id)
        self.assertFalse(created)
        self.assertEqual(replay["id"], release["id"])

        # 重启前撤回的授权仍然阻断签发。
        cand2 = self.service.create_candidate(
            "course-2", REGION, STANDARD, {blocked_comp.id: blocked_var.id}
        )
        self.service.submit_review(cand2.id, "alice", "approve")
        self.service.submit_review(cand2.id, "bob", "approve")
        with self.assertRaises(ValidationError):
            self.service.publish(cand2.id)


if __name__ == "__main__":
    unittest.main()
