"""应用服务：合并候选、并行评审、签发约束与发布生命周期。

核心规则：
- 原稿不可变：变体只增不改，候选通过引用组合，平台从不覆盖各方原稿；
- 签发约束：目标地区的版权授权、适用标准、依赖闭包与评审法定人数
  全部满足才允许签发；
- 授权撤回：阻断未来签发，并把包含该授权的已发布版本标记为风险，
  历史记录一律保留，绝不静默删除；
- 内容指纹：变体按指纹去重（幂等上传），发布按指纹幂等（重复签发
  返回同一发布）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import domain
from .domain import (
    CANDIDATE_OPEN,
    CANDIDATE_PUBLISHED,
    DECISION_APPROVE,
    DECISION_REJECT,
    LICENSE_ACTIVE,
    LICENSE_EXPIRED,
    LICENSE_NOT_YET_VALID,
    LICENSE_WITHDRAWN,
    REASON_LICENSE_EXPIRED,
    REASON_LICENSE_WITHDRAWN,
    RELEASE_ACTIVE,
    RELEASE_ROLLED_BACK,
    RELEASE_SUPERSEDED,
    RISK_AT_RISK,
    RISK_OK,
    Candidate,
    Component,
    License,
    Release,
    Review,
    Variant,
)
from .errors import ConflictError, NotFoundError, ValidationError
from .ports import Clock, IdGenerator, system_clock, uuid_id
from .storage import Database


def _covers(values: list[str], target: str) -> bool:
    """授权范围匹配：支持精确值或 "*" 通配。"""
    return "*" in values or target in values


class ReleaseService:
    """数字教学资源联合发布的应用服务。"""

    def __init__(
        self,
        db_path: str,
        clock: Clock | None = None,
        id_gen: IdGenerator | None = None,
        default_required_approvals: int = 2,
    ) -> None:
        self.db = Database(db_path)
        self.clock: Clock = clock or system_clock
        self.id_gen: IdGenerator = id_gen or uuid_id
        self.default_required_approvals = default_required_approvals

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------
    # 资源组件与语言变体
    # ------------------------------------------------------------------
    def create_component(
        self, name: str, kind: str, author: str | None = None, metadata: dict | None = None
    ) -> Component:
        if not name or not kind:
            raise ValidationError("组件名称与类型必填")
        comp = Component(
            id=self.id_gen("cmp"),
            name=name,
            kind=kind,
            author=author,
            metadata=metadata or {},
            created_at=self.clock(),
        )
        with self.db.write_txn() as conn:
            conn.execute(
                "INSERT INTO components (id, name, kind, author, metadata, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (comp.id, comp.name, comp.kind, comp.author,
                 json.dumps(comp.metadata, ensure_ascii=False), comp.created_at),
            )
        return comp

    def get_component(self, component_id: str) -> Component:
        row = self.db.query_one("SELECT * FROM components WHERE id = ?", (component_id,))
        if row is None:
            raise NotFoundError("组件不存在", {"component_id": component_id})
        return Component.from_row(row)

    def list_components(self) -> list[Component]:
        return [Component.from_row(r) for r in self.db.query("SELECT * FROM components ORDER BY created_at, id")]

    def upload_variant(
        self,
        component_id: str,
        language: str,
        content: Any,
        standards: list[str] | None = None,
        metadata: dict | None = None,
        created_by: str | None = None,
    ) -> tuple[Variant, bool]:
        """上传变体元数据与内容。按内容指纹去重：重复上传返回既有变体。

        返回 (variant, created)；created=False 表示命中去重，未产生新行。
        """
        self.get_component(component_id)
        if not language:
            raise ValidationError("变体语言必填")
        standards = list(standards or [])
        fingerprint = domain.variant_fingerprint(language, standards, content)
        existing = self._find_variant_by_fingerprint(component_id, language, fingerprint)
        if existing is not None:
            return existing, False
        variant = Variant(
            id=self.id_gen("var"),
            component_id=component_id,
            language=language,
            standards=standards,
            content=content,
            fingerprint=fingerprint,
            metadata=metadata or {},
            created_by=created_by,
            created_at=self.clock(),
        )
        try:
            with self.db.write_txn() as conn:
                conn.execute(
                    "INSERT INTO variants (id, component_id, language, standards, content,"
                    " fingerprint, metadata, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        variant.id, variant.component_id, variant.language,
                        json.dumps(variant.standards, ensure_ascii=False),
                        json.dumps(variant.content, ensure_ascii=False),
                        variant.fingerprint,
                        json.dumps(variant.metadata, ensure_ascii=False),
                        variant.created_by, variant.created_at,
                    ),
                )
        except sqlite3.IntegrityError:
            # 并发上传同一内容：另一事务已落盘，返回既有变体（幂等）。
            raced = self._find_variant_by_fingerprint(component_id, language, fingerprint)
            if raced is not None:
                return raced, False
            raise
        return variant, True

    def list_variants(self, component_id: str) -> list[Variant]:
        self.get_component(component_id)
        rows = self.db.query(
            "SELECT * FROM variants WHERE component_id = ? ORDER BY created_at, id",
            (component_id,),
        )
        return [Variant.from_row(r) for r in rows]

    def variant_conflicts(self, component_id: str) -> list[dict[str, Any]]:
        """列出同一语言下存在多个不同内容版本的冲突（供运营方决议）。"""
        variants = self.list_variants(component_id)
        by_language: dict[str, list[Variant]] = {}
        for v in variants:
            by_language.setdefault(v.language, []).append(v)
        conflicts = []
        for language, group in sorted(by_language.items()):
            if len({v.fingerprint for v in group}) > 1:
                conflicts.append(
                    {
                        "component_id": component_id,
                        "language": language,
                        "variants": [
                            {"variant_id": v.id, "fingerprint": v.fingerprint,
                             "created_by": v.created_by, "created_at": v.created_at}
                            for v in group
                        ],
                    }
                )
        return conflicts

    def _find_variant_by_fingerprint(
        self, component_id: str, language: str, fingerprint: str
    ) -> Variant | None:
        row = self.db.query_one(
            "SELECT * FROM variants WHERE component_id = ? AND language = ? AND fingerprint = ?",
            (component_id, language, fingerprint),
        )
        return Variant.from_row(row) if row else None

    def _get_variant(self, variant_id: str) -> Variant:
        row = self.db.query_one("SELECT * FROM variants WHERE id = ?", (variant_id,))
        if row is None:
            raise NotFoundError("变体不存在", {"variant_id": variant_id})
        return Variant.from_row(row)

    # ------------------------------------------------------------------
    # 版权授权
    # ------------------------------------------------------------------
    def grant_license(
        self,
        component_id: str,
        licensor: str | None = None,
        regions: list[str] | None = None,
        standards: list[str] | None = None,
        valid_from: float | None = None,
        valid_until: float | None = None,
    ) -> License:
        self.get_component(component_id)
        regions = list(regions or [])
        standards = list(standards or [])
        if not regions:
            raise ValidationError("授权必须指定适用地区")
        if valid_from is not None and valid_until is not None and valid_from > valid_until:
            raise ValidationError("授权有效期起止颠倒")
        lic = License(
            id=self.id_gen("lic"),
            component_id=component_id,
            licensor=licensor,
            regions=regions,
            standards=standards,
            valid_from=valid_from,
            valid_until=valid_until,
            status=LICENSE_ACTIVE,
            withdrawn_at=None,
            withdraw_reason=None,
            created_at=self.clock(),
        )
        with self.db.write_txn() as conn:
            conn.execute(
                "INSERT INTO licenses (id, component_id, licensor, regions, standards,"
                " valid_from, valid_until, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    lic.id, lic.component_id, lic.licensor,
                    json.dumps(lic.regions, ensure_ascii=False),
                    json.dumps(lic.standards, ensure_ascii=False),
                    lic.valid_from, lic.valid_until, lic.status, lic.created_at,
                ),
            )
        return lic

    def get_license(self, license_id: str) -> License:
        row = self.db.query_one("SELECT * FROM licenses WHERE id = ?", (license_id,))
        if row is None:
            raise NotFoundError("授权不存在", {"license_id": license_id})
        return License.from_row(row)

    def list_licenses(self, component_id: str | None = None) -> list[License]:
        if component_id is None:
            rows = self.db.query("SELECT * FROM licenses ORDER BY created_at, id")
        else:
            rows = self.db.query(
                "SELECT * FROM licenses WHERE component_id = ? ORDER BY created_at, id",
                (component_id,),
            )
        return [License.from_row(r) for r in rows]

    def license_state(self, lic: License, now: float | None = None) -> str:
        """授权在给定时刻的有效状态（expired / not_yet_valid 为派生状态）。"""
        now = self.clock() if now is None else now
        if lic.status == LICENSE_WITHDRAWN:
            return LICENSE_WITHDRAWN
        if lic.valid_until is not None and now > lic.valid_until:
            return LICENSE_EXPIRED
        if lic.valid_from is not None and now < lic.valid_from:
            return LICENSE_NOT_YET_VALID
        return LICENSE_ACTIVE

    def withdraw_license(self, license_id: str, reason: str | None = None) -> dict[str, Any]:
        """撤回授权：阻断未来签发，并把包含该授权的已发布版本标记为风险。

        历史一律保留：发布、快照与授权行均不删除。重复撤回幂等。
        """
        now = self.clock()
        with self.db.write_txn() as conn:
            row = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
            if row is None:
                raise NotFoundError("授权不存在", {"license_id": license_id})
            lic = License.from_row(row)
            if lic.status == LICENSE_WITHDRAWN:
                return {"license": lic, "affected_release_ids": []}
            conn.execute(
                "UPDATE licenses SET status = ?, withdrawn_at = ?, withdraw_reason = ? WHERE id = ?",
                (LICENSE_WITHDRAWN, now, reason, license_id),
            )
            affected: list[str] = []
            for rel_row in conn.execute("SELECT * FROM releases").fetchall():
                rel = Release.from_row(rel_row)
                license_ids = {s.get("license_id") for s in rel.snapshot.get("selections", [])}
                if license_id not in license_ids:
                    continue
                reasons = list(rel.risk_reasons)
                if not any(
                    r.get("type") == REASON_LICENSE_WITHDRAWN and r.get("license_id") == license_id
                    for r in reasons
                ):
                    reasons.append(
                        {
                            "type": REASON_LICENSE_WITHDRAWN,
                            "license_id": license_id,
                            "reason": reason,
                            "withdrawn_at": now,
                        }
                    )
                    conn.execute(
                        "UPDATE releases SET risk_status = ?, risk_reasons = ? WHERE id = ?",
                        (RISK_AT_RISK, json.dumps(reasons, ensure_ascii=False), rel.id),
                    )
                affected.append(rel.id)
            lic = License.from_row(
                conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
            )
        return {"license": lic, "affected_release_ids": affected}

    # ------------------------------------------------------------------
    # 依赖关系
    # ------------------------------------------------------------------
    def add_dependency(
        self, component_id: str, depends_on: str, note: str | None = None
    ) -> dict[str, Any]:
        """登记组件依赖（如字幕组件依赖视频组件）。拒绝成环。重复登记幂等。"""
        if component_id == depends_on:
            raise ValidationError("组件不能依赖自身")
        self.get_component(component_id)
        self.get_component(depends_on)
        if component_id in self._dependency_closure({depends_on}):
            raise ValidationError(
                "依赖会形成环", {"component_id": component_id, "depends_on": depends_on}
            )
        with self.db.write_txn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dependencies (component_id, depends_on, note) VALUES (?, ?, ?)",
                (component_id, depends_on, note),
            )
            created = cursor.rowcount > 0
        return {"component_id": component_id, "depends_on": depends_on,
                "note": note, "created": created}

    def dependencies_of(self, component_id: str) -> list[str]:
        rows = self.db.query(
            "SELECT depends_on FROM dependencies WHERE component_id = ? ORDER BY depends_on",
            (component_id,),
        )
        return [r["depends_on"] for r in rows]

    def _dependency_closure(self, roots: set[str]) -> set[str]:
        """依赖传递闭包（含根）。用于签发前校验依赖组件全部入选。"""
        seen = set(roots)
        stack = list(roots)
        while stack:
            current = stack.pop()
            for dep in self.dependencies_of(current):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        return seen

    # ------------------------------------------------------------------
    # 合并候选与并行评审
    # ------------------------------------------------------------------
    def create_candidate(
        self,
        course_id: str,
        target_region: str,
        target_standard: str,
        selections: dict[str, str],
        title: str | None = None,
        created_by: str | None = None,
        required_approvals: int | None = None,
    ) -> Candidate:
        """建立合并候选：显式选择每个组件的变体，不修改任何原稿。

        selections: {component_id: variant_id}。依赖闭包必须全部覆盖。
        """
        if not course_id or not target_region or not target_standard:
            raise ValidationError("课程、目标地区与适用标准必填")
        if not selections:
            raise ValidationError("候选至少选择一个组件变体")
        for comp_id, var_id in selections.items():
            self.get_component(comp_id)
            variant = self._get_variant(var_id)
            if variant.component_id != comp_id:
                raise ValidationError(
                    "变体不属于对应组件", {"component_id": comp_id, "variant_id": var_id}
                )
        missing = sorted(self._dependency_closure(set(selections)) - set(selections))
        if missing:
            raise ValidationError("依赖组件未全部入选", {"missing_components": missing})
        cand = Candidate(
            id=self.id_gen("cand"),
            course_id=course_id,
            title=title,
            target_region=target_region,
            target_standard=target_standard,
            required_approvals=required_approvals or self.default_required_approvals,
            status=CANDIDATE_OPEN,
            created_by=created_by,
            created_at=self.clock(),
            selections=dict(selections),
        )
        with self.db.write_txn() as conn:
            conn.execute(
                "INSERT INTO candidates (id, course_id, title, target_region, target_standard,"
                " required_approvals, status, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cand.id, cand.course_id, cand.title, cand.target_region,
                    cand.target_standard, cand.required_approvals, cand.status,
                    cand.created_by, cand.created_at,
                ),
            )
            for comp_id, var_id in selections.items():
                conn.execute(
                    "INSERT INTO candidate_selections (candidate_id, component_id, variant_id)"
                    " VALUES (?, ?, ?)",
                    (cand.id, comp_id, var_id),
                )
        return cand

    def get_candidate(self, candidate_id: str) -> Candidate:
        row = self.db.query_one("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
        if row is None:
            raise NotFoundError("候选不存在", {"candidate_id": candidate_id})
        selections = {
            r["component_id"]: r["variant_id"]
            for r in self.db.query(
                "SELECT component_id, variant_id FROM candidate_selections WHERE candidate_id = ?",
                (candidate_id,),
            )
        }
        return Candidate.from_row(row, selections)

    def submit_review(
        self, candidate_id: str, reviewer: str, decision: str, comment: str | None = None
    ) -> Review:
        """提交评审意见。评审可并行进行；同一评审人以最新意见为准（冲突决议）。"""
        self.get_candidate(candidate_id)
        if not reviewer:
            raise ValidationError("评审人必填")
        if decision not in (DECISION_APPROVE, DECISION_REJECT):
            raise ValidationError("评审决定必须为 approve 或 reject")
        review = Review(
            id=self.id_gen("rev"),
            candidate_id=candidate_id,
            reviewer=reviewer,
            decision=decision,
            comment=comment,
            created_at=self.clock(),
        )
        with self.db.write_txn() as conn:
            conn.execute(
                "INSERT INTO reviews (id, candidate_id, reviewer, decision, comment, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (review.id, review.candidate_id, review.reviewer,
                 review.decision, review.comment, review.created_at),
            )
        return review

    def list_reviews(self, candidate_id: str) -> list[Review]:
        rows = self.db.query(
            "SELECT * FROM reviews WHERE candidate_id = ? ORDER BY created_at, rowid",
            (candidate_id,),
        )
        return [Review.from_row(r) for r in rows]

    def evaluate_candidate(self, candidate_id: str) -> dict[str, Any]:
        """评估候选是否满足目标地区全部签发约束，返回结构化报告。"""
        cand = self.get_candidate(candidate_id)
        now = self.clock()
        issues: list[dict[str, Any]] = []

        missing = sorted(self._dependency_closure(set(cand.selections)) - set(cand.selections))
        for comp_id in missing:
            issues.append({"type": "missing_dependency_selection", "component_id": comp_id})

        for comp_id, var_id in sorted(cand.selections.items()):
            variant = self._get_variant(var_id)
            if not _covers(variant.standards, cand.target_standard):
                issues.append(
                    {
                        "type": "standard_not_supported",
                        "component_id": comp_id,
                        "variant_id": var_id,
                        "standard": cand.target_standard,
                    }
                )
            license_issue = self._license_issue(comp_id, cand.target_region,
                                                cand.target_standard, now)
            if license_issue is not None:
                issues.append(license_issue)

        latest = self._latest_reviews(candidate_id)
        approvals = sorted(r for r, rev in latest.items() if rev.decision == DECISION_APPROVE)
        rejections = sorted(r for r, rev in latest.items() if rev.decision == DECISION_REJECT)
        if len(approvals) < cand.required_approvals:
            issues.append(
                {
                    "type": "insufficient_approvals",
                    "have": len(approvals),
                    "need": cand.required_approvals,
                }
            )
        if rejections:
            issues.append({"type": "standing_rejections", "reviewers": rejections})

        return {
            "candidate_id": candidate_id,
            "publishable": not issues,
            "issues": issues,
            "approvals": approvals,
            "rejections": rejections,
            "required_approvals": cand.required_approvals,
        }

    def _latest_reviews(self, candidate_id: str) -> dict[str, Review]:
        """每位评审人的最新意见（并行评审的冲突决议规则）。"""
        latest: dict[str, Review] = {}
        for review in self.list_reviews(candidate_id):
            latest[review.reviewer] = review
        return latest

    def _license_issue(
        self, component_id: str, region: str, standard: str, now: float
    ) -> dict[str, Any] | None:
        """组件在目标地区/标准下的授权问题；无问题返回 None。"""
        licenses = self.list_licenses(component_id)
        covering = [
            lic for lic in licenses
            if _covers(lic.regions, region) and _covers(lic.standards, standard)
        ]
        if any(self.license_state(lic, now) == LICENSE_ACTIVE for lic in covering):
            return None
        if not licenses:
            return {"type": "license_missing", "component_id": component_id}
        if not covering:
            return {
                "type": "license_scope_uncovered",
                "component_id": component_id,
                "region": region,
                "standard": standard,
            }
        states = {self.license_state(lic, now) for lic in covering}
        if LICENSE_WITHDRAWN in states:
            return {"type": "license_withdrawn", "component_id": component_id}
        if LICENSE_EXPIRED in states:
            return {"type": "license_expired", "component_id": component_id}
        return {"type": "license_not_yet_valid", "component_id": component_id}

    # ------------------------------------------------------------------
    # 发布与回滚
    # ------------------------------------------------------------------
    def publish(self, candidate_id: str, issued_by: str | None = None) -> tuple[dict[str, Any], bool]:
        """签发候选为发布版本。

        约束全部满足才签发；同一候选重复签发幂等（返回同一发布）；
        同一课程/地区/标准的新签发取代旧发布（旧版本保留为 superseded）。
        返回 (release_view, created)。
        """
        with self.db.write_txn() as conn:
            cand = self.get_candidate(candidate_id)
            report = self.evaluate_candidate(candidate_id)
            if not report["publishable"]:
                raise ValidationError("候选未满足签发约束", report)

            variant_fps: dict[str, str] = {}
            snapshot_selections: list[dict[str, Any]] = []
            now = self.clock()
            for comp_id, var_id in sorted(cand.selections.items()):
                variant = self._get_variant(var_id)
                variant_fps[comp_id] = variant.fingerprint
                license_id = self._covering_license_id(
                    comp_id, cand.target_region, cand.target_standard, now
                )
                snapshot_selections.append(
                    {
                        "component_id": comp_id,
                        "variant_id": var_id,
                        "variant_fingerprint": variant.fingerprint,
                        "language": variant.language,
                        "license_id": license_id,
                    }
                )
            fingerprint = domain.release_fingerprint(
                cand.course_id, cand.target_region, cand.target_standard, variant_fps
            )

            row = conn.execute(
                "SELECT * FROM releases WHERE course_id = ? AND target_region = ?"
                " AND target_standard = ? AND status = ? AND fingerprint = ?",
                (cand.course_id, cand.target_region, cand.target_standard,
                 RELEASE_ACTIVE, fingerprint),
            ).fetchone()
            if row is not None:
                return self._release_view(Release.from_row(row)), False

            conn.execute(
                "UPDATE releases SET status = ? WHERE course_id = ? AND target_region = ?"
                " AND target_standard = ? AND status = ?",
                (RELEASE_SUPERSEDED, cand.course_id, cand.target_region,
                 cand.target_standard, RELEASE_ACTIVE),
            )
            release = Release(
                id=self.id_gen("rel"),
                candidate_id=cand.id,
                course_id=cand.course_id,
                target_region=cand.target_region,
                target_standard=cand.target_standard,
                fingerprint=fingerprint,
                snapshot={
                    "candidate_id": cand.id,
                    "course_id": cand.course_id,
                    "target_region": cand.target_region,
                    "target_standard": cand.target_standard,
                    "selections": snapshot_selections,
                    "approvals": report["approvals"],
                    "required_approvals": cand.required_approvals,
                },
                status=RELEASE_ACTIVE,
                risk_status=RISK_OK,
                risk_reasons=[],
                issued_by=issued_by,
                issued_at=now,
                rolled_back_at=None,
                rollback_reason=None,
            )
            try:
                conn.execute(
                    "INSERT INTO releases (id, candidate_id, course_id, target_region,"
                    " target_standard, fingerprint, snapshot, status, risk_status,"
                    " risk_reasons, issued_by, issued_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        release.id, release.candidate_id, release.course_id,
                        release.target_region, release.target_standard, release.fingerprint,
                        json.dumps(release.snapshot, ensure_ascii=False), release.status,
                        release.risk_status, json.dumps(release.risk_reasons, ensure_ascii=False),
                        release.issued_by, release.issued_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "目标地区已存在进行中的发布",
                    {"course_id": cand.course_id, "region": cand.target_region,
                     "standard": cand.target_standard},
                ) from exc
            conn.execute(
                "UPDATE candidates SET status = ? WHERE id = ?",
                (CANDIDATE_PUBLISHED, cand.id),
            )
        return self._release_view(release), True

    def rollback(self, release_id: str, reason: str | None = None) -> dict[str, Any]:
        """回滚进行中的发布：状态转为 rolled_back，历史保留，不自动恢复旧版。"""
        with self.db.write_txn() as conn:
            row = conn.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
            if row is None:
                raise NotFoundError("发布不存在", {"release_id": release_id})
            rel = Release.from_row(row)
            if rel.status != RELEASE_ACTIVE:
                raise ConflictError(
                    "仅进行中的发布可回滚", {"release_id": release_id, "status": rel.status}
                )
            conn.execute(
                "UPDATE releases SET status = ?, rolled_back_at = ?, rollback_reason = ?"
                " WHERE id = ?",
                (RELEASE_ROLLED_BACK, self.clock(), reason, release_id),
            )
            rel = Release.from_row(
                conn.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
            )
        return self._release_view(rel)

    def get_release(self, release_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM releases WHERE id = ?", (release_id,))
        if row is None:
            raise NotFoundError("发布不存在", {"release_id": release_id})
        return self._release_view(Release.from_row(row))

    def list_releases(self, course_id: str | None = None) -> list[dict[str, Any]]:
        if course_id is None:
            rows = self.db.query("SELECT * FROM releases ORDER BY issued_at, id")
        else:
            rows = self.db.query(
                "SELECT * FROM releases WHERE course_id = ? ORDER BY issued_at, id", (course_id,)
            )
        return [self._release_view(Release.from_row(r)) for r in rows]

    def _covering_license_id(
        self, component_id: str, region: str, standard: str, now: float
    ) -> str | None:
        """签发时刻覆盖目标地区/标准的有效授权（确定性取最早创建者）。"""
        for lic in self.list_licenses(component_id):
            if (
                _covers(lic.regions, region)
                and _covers(lic.standards, standard)
                and self.license_state(lic, now) == LICENSE_ACTIVE
            ):
                return lic.id
        return None

    def _release_view(self, rel: Release) -> dict[str, Any]:
        """发布视图：合并存储的风险标记与按当前时间派生的授权风险。"""
        view = rel.to_dict()
        reasons = [dict(r) for r in rel.risk_reasons]
        seen = {(r.get("type"), r.get("license_id")) for r in reasons}
        now = self.clock()
        for sel in rel.snapshot.get("selections", []):
            license_id = sel.get("license_id")
            if not license_id:
                continue
            try:
                lic = self.get_license(license_id)
            except NotFoundError:
                continue
            state = self.license_state(lic, now)
            if state == LICENSE_WITHDRAWN and (REASON_LICENSE_WITHDRAWN, lic.id) not in seen:
                reasons.append(
                    {
                        "type": REASON_LICENSE_WITHDRAWN,
                        "license_id": lic.id,
                        "reason": lic.withdraw_reason,
                        "withdrawn_at": lic.withdrawn_at,
                    }
                )
                seen.add((REASON_LICENSE_WITHDRAWN, lic.id))
            elif state == LICENSE_EXPIRED and (REASON_LICENSE_EXPIRED, lic.id) not in seen:
                reasons.append(
                    {
                        "type": REASON_LICENSE_EXPIRED,
                        "license_id": lic.id,
                        "valid_until": lic.valid_until,
                    }
                )
                seen.add((REASON_LICENSE_EXPIRED, lic.id))
        view["risk_reasons"] = reasons
        view["risk_status"] = RISK_AT_RISK if reasons else RISK_OK
        return view
