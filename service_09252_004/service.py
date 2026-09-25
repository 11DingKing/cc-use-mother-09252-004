"""应用服务层：资源组件、语言变体、版权授权、审核意见、依赖关系、合并候选与签发回滚。

设计要点：
- 组件与变体按内容指纹去重，重复上传返回既有记录（幂等），原稿永不被覆盖；
- 合并候选只是对原稿的引用组合，不改动任何上传内容；
- 签发前校验目标地区全部约束（授权有效且覆盖地区、依赖满足、评审通过、标准齐备），
  任一不满足即拒绝并列出全部违例；
- 授权撤回/到期会阻断未来签发，并把受影响的已发布版本标记为 at_risk；
  所有历史（授权、版本、事件）一律保留，绝不静默删除。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .errors import (
    ConflictError,
    ConstraintViolation,
    NotFoundError,
    ValidationError,
)
from .fingerprint import content_fingerprint
from .ports import Clock, IdGenerator, SystemClock, UuidIds
from .storage import Storage

DECISIONS = ("approve", "reject")
DEFAULT_REQUIRED_APPROVALS = 1


# ---------------------------------------------------------------- 入参校验


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{field} must be a non-empty string", details={"field": field}
        )
    return value.strip()


def _require_str_list(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(v, str) or not v.strip() for v in value
    ):
        raise ValidationError(
            f"{field} must be a list of non-empty strings", details={"field": field}
        )
    items = sorted({v.strip() for v in value})
    if not items and not allow_empty:
        raise ValidationError(f"{field} must not be empty", details={"field": field})
    return items


def _require_payload(value: Any, field: str = "payload") -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(
            f"{field} must be a JSON object", details={"field": field}
        )
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        raise ValidationError(
            f"{field} must be JSON serializable", details={"field": field}
        )
    return value


def _optional_time(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(
            f"{field} must be a unix timestamp (number)", details={"field": field}
        )
    return float(value)


# ---------------------------------------------------------------- 行 → 字典


def _component_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "component_id": row["component_id"],
        "fingerprint": row["fingerprint"],
        "title": row["title"],
        "author": row["author"],
        "origin_region": row["origin_region"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }


def _variant_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "variant_id": row["variant_id"],
        "component_id": row["component_id"],
        "language": row["language"],
        "region": row["region"],
        "subtitles": json.loads(row["subtitles"]),
        "cases": json.loads(row["cases"]),
        "payload": json.loads(row["payload"]),
        "fingerprint": row["fingerprint"],
        "created_at": row["created_at"],
    }


def _license_dict(row: sqlite3.Row, now: float) -> dict[str, Any]:
    status = row["status"]
    valid_from, valid_until = row["valid_from"], row["valid_until"]
    if status == "withdrawn":
        effective = "withdrawn"
    elif valid_until is not None and valid_until <= now:
        effective = "expired"
    elif valid_from is not None and valid_from > now:
        effective = "not_yet_valid"
    else:
        effective = "active"
    return {
        "license_id": row["license_id"],
        "component_id": row["component_id"],
        "licensor": row["licensor"],
        "regions": json.loads(row["regions"]),
        "standards": json.loads(row["standards"]),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "status": status,
        "effective_status": effective,
        "withdrawn_at": row["withdrawn_at"],
        "created_at": row["created_at"],
    }


def _review_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "review_id": row["review_id"],
        "candidate_id": row["candidate_id"],
        "reviewer": row["reviewer"],
        "scope": row["scope"],
        "decision": row["decision"],
        "comment": row["comment"],
        "created_at": row["created_at"],
    }


def _release_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "release_id": row["release_id"],
        "candidate_id": row["candidate_id"],
        "region": row["region"],
        "status": row["status"],
        "signed_at": row["signed_at"],
        "rolled_back_at": row["rolled_back_at"],
        "risk_reason": json.loads(row["risk_reason"]) if row["risk_reason"] else None,
    }


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "release_id": row["release_id"],
        "event": row["event"],
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 应用服务


class CourseHubService:
    """数字教学资源联合发布的应用服务。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Optional[Clock] = None,
        ids: Optional[IdGenerator] = None,
    ):
        self.storage = Storage(db_path)
        self.clock = clock or SystemClock()
        self.ids = ids or UuidIds()

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> "CourseHubService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 上传（幂等去重）

    def upload_component(
        self,
        *,
        title: str,
        author: str,
        origin_region: str,
        payload: Optional[dict] = None,
    ) -> dict[str, Any]:
        """上传资源组件元数据。内容指纹相同则返回既有记录，不覆盖原稿。"""
        title = _require_str(title, "title")
        author = _require_str(author, "author")
        origin_region = _require_str(origin_region, "origin_region")
        payload = _require_payload(payload)
        fingerprint = content_fingerprint(
            {
                "kind": "component",
                "title": title,
                "author": author,
                "origin_region": origin_region,
                "payload": payload,
            }
        )
        with self.storage.transaction():
            row = self.storage.one(
                "SELECT * FROM components WHERE fingerprint=?", (fingerprint,)
            )
            if row is not None:
                return {"component": _component_dict(row), "created": False}
            component_id = self.ids.new_id("cmp")
            try:
                self.storage.write(
                    "INSERT INTO components(component_id, fingerprint, title, author,"
                    " origin_region, payload, created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        component_id,
                        fingerprint,
                        title,
                        author,
                        origin_region,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        self.clock.now(),
                    ),
                )
            except sqlite3.IntegrityError:  # 并发下唯一约束兜底
                row = self.storage.one(
                    "SELECT * FROM components WHERE fingerprint=?", (fingerprint,)
                )
                return {"component": _component_dict(row), "created": False}
            row = self.storage.one(
                "SELECT * FROM components WHERE component_id=?", (component_id,)
            )
            return {"component": _component_dict(row), "created": True}

    def upload_variant(
        self,
        *,
        component_id: str,
        language: str,
        region: str,
        subtitles: Optional[list[str]] = None,
        cases: Optional[list[str]] = None,
        payload: Optional[dict] = None,
    ) -> dict[str, Any]:
        """为组件上传语言变体（字幕、本地化案例等）。同样按指纹去重。"""
        self._component_or_404(component_id)
        language = _require_str(language, "language")
        region = _require_str(region, "region")
        subtitles = _require_str_list(subtitles or [], "subtitles")
        cases = _require_str_list(cases or [], "cases")
        payload = _require_payload(payload)
        fingerprint = content_fingerprint(
            {
                "kind": "variant",
                "component_id": component_id,
                "language": language,
                "region": region,
                "subtitles": subtitles,
                "cases": cases,
                "payload": payload,
            }
        )
        with self.storage.transaction():
            row = self.storage.one(
                "SELECT * FROM variants WHERE fingerprint=?", (fingerprint,)
            )
            if row is not None:
                return {"variant": _variant_dict(row), "created": False}
            variant_id = self.ids.new_id("var")
            try:
                self.storage.write(
                    "INSERT INTO variants(variant_id, component_id, language, region,"
                    " subtitles, cases, payload, fingerprint, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        variant_id,
                        component_id,
                        language,
                        region,
                        json.dumps(subtitles, ensure_ascii=False),
                        json.dumps(cases, ensure_ascii=False),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        fingerprint,
                        self.clock.now(),
                    ),
                )
            except sqlite3.IntegrityError:
                row = self.storage.one(
                    "SELECT * FROM variants WHERE fingerprint=?", (fingerprint,)
                )
                return {"variant": _variant_dict(row), "created": False}
            row = self.storage.one(
                "SELECT * FROM variants WHERE variant_id=?", (variant_id,)
            )
            return {"variant": _variant_dict(row), "created": True}

    # ------------------------------------------------------------ 版权授权

    def register_license(
        self,
        *,
        component_id: str,
        licensor: str,
        regions: list[str],
        standards: Optional[list[str]] = None,
        valid_from: Optional[float] = None,
        valid_until: Optional[float] = None,
    ) -> dict[str, Any]:
        """登记版权授权。与既有有效授权完全相同的重复登记会被去重。"""
        self._component_or_404(component_id)
        licensor = _require_str(licensor, "licensor")
        regions = _require_str_list(regions, "regions", allow_empty=False)
        standards = _require_str_list(standards or [], "standards")
        valid_from = _optional_time(valid_from, "valid_from")
        valid_until = _optional_time(valid_until, "valid_until")
        if (
            valid_from is not None
            and valid_until is not None
            and valid_until <= valid_from
        ):
            raise ValidationError("valid_until must be later than valid_from")
        with self.storage.transaction():
            for row in self.storage.all(
                "SELECT * FROM licenses WHERE component_id=? AND status='active'",
                (component_id,),
            ):
                lic = _license_dict(row, self.clock.now())
                if (
                    lic["licensor"] == licensor
                    and lic["regions"] == regions
                    and lic["standards"] == standards
                    and lic["valid_from"] == valid_from
                    and lic["valid_until"] == valid_until
                ):
                    return {"license": lic, "created": False}
            license_id = self.ids.new_id("lic")
            self.storage.write(
                "INSERT INTO licenses(license_id, component_id, licensor, regions,"
                " standards, valid_from, valid_until, status, created_at)"
                " VALUES(?,?,?,?,?,?,?, 'active', ?)",
                (
                    license_id,
                    component_id,
                    licensor,
                    json.dumps(regions, ensure_ascii=False),
                    json.dumps(standards, ensure_ascii=False),
                    valid_from,
                    valid_until,
                    self.clock.now(),
                ),
            )
            row = self.storage.one(
                "SELECT * FROM licenses WHERE license_id=?", (license_id,)
            )
            return {"license": _license_dict(row, self.clock.now()), "created": True}

    def withdraw_license(self, license_id: str) -> dict[str, Any]:
        """撤回授权：阻断未来签发，并把受影响的已发布版本标记为 at_risk。

        授权记录本身保留（status=withdrawn），历史不删除。重复撤回是幂等空操作。
        """
        with self.storage.transaction():
            row = self.storage.one(
                "SELECT * FROM licenses WHERE license_id=?", (license_id,)
            )
            if row is None:
                raise NotFoundError(
                    "license not found", details={"license_id": license_id}
                )
            now = self.clock.now()
            if row["status"] == "withdrawn":
                return {
                    "license": _license_dict(row, now),
                    "changed": False,
                    "at_risk_release_ids": [],
                }
            self.storage.write(
                "UPDATE licenses SET status='withdrawn', withdrawn_at=?"
                " WHERE license_id=?",
                (now, license_id),
            )
            affected = self._refresh_risks(now)
            row = self.storage.one(
                "SELECT * FROM licenses WHERE license_id=?", (license_id,)
            )
            return {
                "license": _license_dict(row, now),
                "changed": True,
                "at_risk_release_ids": affected,
            }

    # ------------------------------------------------------------ 依赖关系

    def add_dependency(
        self,
        *,
        component_id: str,
        depends_on_id: str,
        required_fingerprint: Optional[str] = None,
    ) -> dict[str, Any]:
        """登记组件依赖。拒绝自依赖与循环依赖；重复登记更新指纹钉扎。"""
        component_id = _require_str(component_id, "component_id")
        depends_on_id = _require_str(depends_on_id, "depends_on_id")
        if component_id == depends_on_id:
            raise ValidationError("component cannot depend on itself")
        self._component_or_404(component_id)
        self._component_or_404(depends_on_id)
        if required_fingerprint is not None:
            required_fingerprint = _require_str(
                required_fingerprint, "required_fingerprint"
            )
        with self.storage.transaction():
            existing = self.storage.one(
                "SELECT * FROM dependencies WHERE component_id=? AND depends_on_id=?",
                (component_id, depends_on_id),
            )
            if existing is None and self._reachable(depends_on_id, component_id):
                raise ConflictError(
                    "dependency cycle detected",
                    details={
                        "component_id": component_id,
                        "depends_on_id": depends_on_id,
                    },
                )
            self.storage.write(
                "INSERT INTO dependencies(component_id, depends_on_id,"
                " required_fingerprint, created_at) VALUES(?,?,?,?)"
                " ON CONFLICT(component_id, depends_on_id)"
                " DO UPDATE SET required_fingerprint=excluded.required_fingerprint",
                (component_id, depends_on_id, required_fingerprint, self.clock.now()),
            )
            return {
                "dependency": {
                    "component_id": component_id,
                    "depends_on_id": depends_on_id,
                    "required_fingerprint": required_fingerprint,
                },
                "created": existing is None,
            }

    # ------------------------------------------------------------ 地区约束

    def set_region_policy(
        self,
        region: str,
        *,
        required_standards: Optional[list[str]] = None,
        required_approvals: int = DEFAULT_REQUIRED_APPROVALS,
    ) -> dict[str, Any]:
        """设置目标地区约束：必须覆盖的适用标准与最少评审通过人数。"""
        region = _require_str(region, "region")
        standards = _require_str_list(required_standards or [], "required_standards")
        if (
            isinstance(required_approvals, bool)
            or not isinstance(required_approvals, int)
            or required_approvals < 1
        ):
            raise ValidationError("required_approvals must be a positive integer")
        with self.storage.transaction():
            self.storage.write(
                "INSERT INTO region_policies(region, required_standards,"
                " required_approvals, updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(region) DO UPDATE SET"
                " required_standards=excluded.required_standards,"
                " required_approvals=excluded.required_approvals,"
                " updated_at=excluded.updated_at",
                (
                    region,
                    json.dumps(standards, ensure_ascii=False),
                    required_approvals,
                    self.clock.now(),
                ),
            )
            return {"policy": self.get_region_policy(region)}

    def get_region_policy(self, region: str) -> dict[str, Any]:
        row = self.storage.one(
            "SELECT * FROM region_policies WHERE region=?", (region,)
        )
        if row is None:
            return {
                "region": region,
                "required_standards": [],
                "required_approvals": DEFAULT_REQUIRED_APPROVALS,
                "configured": False,
            }
        return {
            "region": row["region"],
            "required_standards": json.loads(row["required_standards"]),
            "required_approvals": row["required_approvals"],
            "configured": True,
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------ 合并候选

    def create_candidate(
        self, *, region: str, variant_ids: list[str]
    ) -> dict[str, Any]:
        """把一组语言变体合并为面向目标地区的发布候选。

        只引用原稿、不做任何修改；同一组件的两个变体不能同入一个候选；
        指纹 = 地区 + 变体集合，相同组合重复合并返回既有候选（幂等）。
        """
        region = _require_str(region, "region")
        variant_ids = _require_str_list(variant_ids, "variant_ids", allow_empty=False)
        with self.storage.transaction():
            marks = ",".join("?" * len(variant_ids))
            rows = self.storage.all(
                f"SELECT * FROM variants WHERE variant_id IN ({marks})",
                tuple(variant_ids),
            )
            found = {r["variant_id"] for r in rows}
            missing = [v for v in variant_ids if v not in found]
            if missing:
                raise NotFoundError(
                    "variants not found", details={"variant_ids": missing}
                )
            seen: dict[str, str] = {}
            duplicated: list[str] = []
            for r in rows:
                if r["component_id"] in seen:
                    duplicated.append(r["component_id"])
                seen[r["component_id"]] = r["variant_id"]
            if duplicated:
                raise ConflictError(
                    "candidate contains multiple variants of the same component",
                    details={"component_ids": sorted(set(duplicated))},
                )
            fingerprint = content_fingerprint(
                {"kind": "candidate", "region": region, "variant_ids": variant_ids}
            )
            row = self.storage.one(
                "SELECT * FROM candidates WHERE fingerprint=?", (fingerprint,)
            )
            if row is not None:
                return {"candidate": self._candidate_dict(row), "created": False}
            candidate_id = self.ids.new_id("cand")
            try:
                self.storage.write(
                    "INSERT INTO candidates(candidate_id, region, fingerprint,"
                    " created_at) VALUES(?,?,?,?)",
                    (candidate_id, region, fingerprint, self.clock.now()),
                )
            except sqlite3.IntegrityError:
                row = self.storage.one(
                    "SELECT * FROM candidates WHERE fingerprint=?", (fingerprint,)
                )
                return {"candidate": self._candidate_dict(row), "created": False}
            for variant_id in variant_ids:
                self.storage.write(
                    "INSERT INTO candidate_items(candidate_id, variant_id)"
                    " VALUES(?,?)",
                    (candidate_id, variant_id),
                )
            row = self.storage.one(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            )
            return {"candidate": self._candidate_dict(row), "created": True}

    # ------------------------------------------------------------ 并行评审

    def submit_review(
        self,
        *,
        candidate_id: str,
        reviewer: str,
        decision: str,
        scope: Optional[str] = None,
        comment: str = "",
    ) -> dict[str, Any]:
        """提交审核意见。多名评审人可并行提交；同一评审人同一范围以最新一条为准
        （冲突决议通过评审人改判来解除）。完全相同的重复提交是幂等空操作。"""
        candidate = self._get_candidate(candidate_id)
        reviewer = _require_str(reviewer, "reviewer")
        if decision not in DECISIONS:
            raise ValidationError(
                f"decision must be one of {DECISIONS}", details={"decision": decision}
            )
        scope = _require_str(scope, "scope") if scope is not None else candidate["region"]
        if not isinstance(comment, str):
            raise ValidationError("comment must be a string")
        with self.storage.transaction():
            for effective in self._effective_reviews(candidate_id):
                if (
                    effective["reviewer"] == reviewer
                    and effective["scope"] == scope
                    and effective["decision"] == decision
                    and effective["comment"] == comment
                ):
                    return {"review": effective, "created": False}
            review_id = self.ids.new_id("rev")
            self.storage.write(
                "INSERT INTO reviews(review_id, candidate_id, reviewer, scope,"
                " decision, comment, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    review_id,
                    candidate_id,
                    reviewer,
                    scope,
                    decision,
                    comment,
                    self.clock.now(),
                ),
            )
            row = self.storage.one(
                "SELECT * FROM reviews WHERE review_id=?", (review_id,)
            )
            return {"review": _review_dict(row), "created": True}

    # ------------------------------------------------------------ 签发与回滚

    def sign_release(self, *, candidate_id: str) -> dict[str, Any]:
        """签发：仅当候选满足目标地区全部约束时生成已发布版本。

        同一候选已存在有效版本（published/at_risk）时返回既有版本（幂等），
        因此并发签发只会有一个胜出者。
        """
        with self.storage.transaction():
            candidate = self._get_candidate(candidate_id)
            row = self.storage.one(
                "SELECT * FROM releases WHERE candidate_id=?"
                " AND status IN ('published','at_risk')"
                " ORDER BY signed_at DESC LIMIT 1",
                (candidate_id,),
            )
            if row is not None:
                return {"release": _release_dict(row), "created": False}
            violations = self._sign_violations(candidate, self.clock.now())
            if violations:
                raise ConstraintViolation(
                    "目标地区约束未满足，无法签发", violations=violations
                )
            release_id = self.ids.new_id("rel")
            now = self.clock.now()
            self.storage.write(
                "INSERT INTO releases(release_id, candidate_id, region, status,"
                " signed_at) VALUES(?,?,?, 'published', ?)",
                (release_id, candidate_id, candidate["region"], now),
            )
            self._add_event(release_id, "signed", "约束全部满足，签发发布", now)
            row = self.storage.one(
                "SELECT * FROM releases WHERE release_id=?", (release_id,)
            )
            return {"release": _release_dict(row), "created": True}

    def rollback_release(self, release_id: str, *, reason: str = "") -> dict[str, Any]:
        """回滚已发布版本。版本与事件历史保留，可修正后重新签发。"""
        with self.storage.transaction():
            row = self.storage.one(
                "SELECT * FROM releases WHERE release_id=?", (release_id,)
            )
            if row is None:
                raise NotFoundError(
                    "release not found", details={"release_id": release_id}
                )
            if row["status"] == "rolled_back":
                return {"release": _release_dict(row), "changed": False}
            now = self.clock.now()
            self.storage.write(
                "UPDATE releases SET status='rolled_back', rolled_back_at=?"
                " WHERE release_id=?",
                (now, release_id),
            )
            self._add_event(release_id, "rolled_back", reason or "运营回滚", now)
            row = self.storage.one(
                "SELECT * FROM releases WHERE release_id=?", (release_id,)
            )
            return {"release": _release_dict(row), "changed": True}

    # ------------------------------------------------------------ 查询

    def get_component(self, component_id: str) -> dict[str, Any]:
        return _component_dict(self._component_or_404(component_id))

    def get_variant(self, variant_id: str) -> dict[str, Any]:
        row = self.storage.one(
            "SELECT * FROM variants WHERE variant_id=?", (variant_id,)
        )
        if row is None:
            raise NotFoundError(
                "variant not found", details={"variant_id": variant_id}
            )
        return _variant_dict(row)

    def get_license(self, license_id: str) -> dict[str, Any]:
        row = self.storage.one(
            "SELECT * FROM licenses WHERE license_id=?", (license_id,)
        )
        if row is None:
            raise NotFoundError(
                "license not found", details={"license_id": license_id}
            )
        return _license_dict(row, self.clock.now())

    def list_licenses(self, component_id: Optional[str] = None) -> list[dict[str, Any]]:
        now = self.clock.now()
        if component_id is None:
            rows = self.storage.all("SELECT * FROM licenses ORDER BY created_at")
        else:
            rows = self.storage.all(
                "SELECT * FROM licenses WHERE component_id=? ORDER BY created_at",
                (component_id,),
            )
        return [_license_dict(r, now) for r in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate = self._get_candidate(candidate_id)
        candidate["reviews"] = self._effective_reviews(candidate_id)
        return candidate

    def check_candidate(self, candidate_id: str) -> dict[str, Any]:
        """预检：不签发，仅列出该候选当前未满足的目标地区约束。"""
        candidate = self._get_candidate(candidate_id)
        violations = self._sign_violations(candidate, self.clock.now())
        return {
            "candidate_id": candidate_id,
            "region": candidate["region"],
            "signable": not violations,
            "violations": violations,
        }

    def get_release(self, release_id: str) -> dict[str, Any]:
        with self.storage.transaction():
            self._refresh_risks(self.clock.now())
            row = self.storage.one(
                "SELECT * FROM releases WHERE release_id=?", (release_id,)
            )
            if row is None:
                raise NotFoundError(
                    "release not found", details={"release_id": release_id}
                )
            events = [
                _event_dict(r)
                for r in self.storage.all(
                    "SELECT * FROM release_events WHERE release_id=?"
                    " ORDER BY created_at, rowid",
                    (release_id,),
                )
            ]
            return {**_release_dict(row), "events": events}

    def list_releases(
        self,
        *,
        status: Optional[str] = None,
        region: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        with self.storage.transaction():
            self._refresh_risks(self.clock.now())
            sql = "SELECT * FROM releases"
            clauses, params = [], []
            if status is not None:
                clauses.append("status=?")
                params.append(status)
            if region is not None:
                clauses.append("region=?")
                params.append(region)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY signed_at, release_id"
            return [_release_dict(r) for r in self.storage.all(sql, tuple(params))]

    # ------------------------------------------------------------ 内部实现

    def _component_or_404(self, component_id: str) -> sqlite3.Row:
        row = self.storage.one(
            "SELECT * FROM components WHERE component_id=?", (component_id,)
        )
        if row is None:
            raise NotFoundError(
                "component not found", details={"component_id": component_id}
            )
        return row

    def _get_candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self.storage.one(
            "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
        )
        if row is None:
            raise NotFoundError(
                "candidate not found", details={"candidate_id": candidate_id}
            )
        return self._candidate_dict(row)

    def _candidate_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        items = self.storage.all(
            "SELECT v.variant_id AS variant_id, v.component_id AS component_id"
            " FROM candidate_items ci"
            " JOIN variants v ON v.variant_id = ci.variant_id"
            " WHERE ci.candidate_id=? ORDER BY v.variant_id",
            (row["candidate_id"],),
        )
        return {
            "candidate_id": row["candidate_id"],
            "region": row["region"],
            "fingerprint": row["fingerprint"],
            "variant_ids": [i["variant_id"] for i in items],
            "component_ids": sorted({i["component_id"] for i in items}),
            "created_at": row["created_at"],
        }

    def _reachable(self, start: str, target: str) -> bool:
        """沿依赖边从 start 出发能否到达 target（用于成环检测）。"""
        stack, seen = [start], set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(
                r["depends_on_id"]
                for r in self.storage.all(
                    "SELECT depends_on_id FROM dependencies WHERE component_id=?",
                    (node,),
                )
            )
        return False

    def _components_with_deps(self, component_ids: list[str]) -> list[dict[str, Any]]:
        """候选直接组件 + 全部传递依赖组件。"""
        visited: set[str] = set()
        stack = list(component_ids)
        while stack:
            cid = stack.pop()
            if cid in visited:
                continue
            visited.add(cid)
            stack.extend(
                r["depends_on_id"]
                for r in self.storage.all(
                    "SELECT depends_on_id FROM dependencies WHERE component_id=?",
                    (cid,),
                )
            )
        if not visited:
            return []
        marks = ",".join("?" * len(visited))
        rows = self.storage.all(
            f"SELECT * FROM components WHERE component_id IN ({marks})",
            tuple(sorted(visited)),
        )
        return [_component_dict(r) for r in rows]

    def _effective_reviews(self, candidate_id: str) -> list[dict[str, Any]]:
        """每个（评审人, 范围）取最新一条，作为当前有效意见。"""
        rows = self.storage.all(
            "SELECT * FROM reviews WHERE candidate_id=? ORDER BY created_at, rowid",
            (candidate_id,),
        )
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        for row in rows:
            latest[(row["reviewer"], row["scope"])] = row
        return [_review_dict(r) for r in latest.values()]

    def _license_dep_violations(
        self, component_ids: list[str], region: str, now: float
    ) -> list[dict[str, Any]]:
        """授权与依赖违例：每个组件（含传递依赖）须存在覆盖目标地区的有效授权。"""
        components = self._components_with_deps(component_ids)
        by_id = {c["component_id"]: c for c in components}
        violations: list[dict[str, Any]] = []
        for component in components:
            cid = component["component_id"]
            rows = self.storage.all(
                "SELECT * FROM licenses WHERE component_id=?", (cid,)
            )
            causes: list[str] = []
            covered = False
            for row in rows:
                lic = _license_dict(row, now)
                if region not in lic["regions"]:
                    causes.append(f"region_not_covered:{lic['license_id']}")
                elif lic["effective_status"] != "active":
                    causes.append(f"{lic['effective_status']}:{lic['license_id']}")
                else:
                    covered = True
            if not rows:
                causes.append("none_registered")
            if not covered:
                violations.append(
                    {
                        "type": "license_missing",
                        "component_id": cid,
                        "region": region,
                        "causes": causes,
                    }
                )
        for edge in self.storage.all("SELECT * FROM dependencies"):
            if edge["component_id"] not in by_id:
                continue
            pin = edge["required_fingerprint"]
            target = by_id.get(edge["depends_on_id"])
            if pin and target and pin != target["fingerprint"]:
                violations.append(
                    {
                        "type": "dependency_fingerprint_mismatch",
                        "component_id": edge["component_id"],
                        "depends_on_id": edge["depends_on_id"],
                        "required_fingerprint": pin,
                        "actual_fingerprint": target["fingerprint"],
                    }
                )
        return violations

    def _review_violations(
        self, candidate_id: str, region: str
    ) -> list[dict[str, Any]]:
        policy = self.get_region_policy(region)
        violations: list[dict[str, Any]] = []
        effective = self._effective_reviews(candidate_id)
        for review in effective:
            if review["decision"] == "reject":
                violations.append(
                    {
                        "type": "review_rejected",
                        "reviewer": review["reviewer"],
                        "scope": review["scope"],
                        "comment": review["comment"],
                    }
                )
        approvers = {
            r["reviewer"]
            for r in effective
            if r["decision"] == "approve" and r["scope"] == region
        }
        required = policy["required_approvals"]
        if len(approvers) < required:
            violations.append(
                {
                    "type": "reviews_insufficient",
                    "scope": region,
                    "required": required,
                    "actual": len(approvers),
                }
            )
        return violations

    def _standards_violations(
        self, component_ids: list[str], region: str, now: float
    ) -> list[dict[str, Any]]:
        policy = self.get_region_policy(region)
        required = policy["required_standards"]
        if not required:
            return []
        covered: set[str] = set()
        for component in self._components_with_deps(component_ids):
            for row in self.storage.all(
                "SELECT * FROM licenses WHERE component_id=?",
                (component["component_id"],),
            ):
                lic = _license_dict(row, now)
                if region in lic["regions"] and lic["effective_status"] == "active":
                    covered.update(lic["standards"])
        missing = [s for s in required if s not in covered]
        if not missing:
            return []
        return [
            {"type": "standards_missing", "region": region, "missing": missing}
        ]

    def _sign_violations(
        self, candidate: dict[str, Any], now: float
    ) -> list[dict[str, Any]]:
        region = candidate["region"]
        component_ids = candidate["component_ids"]
        violations = self._license_dep_violations(component_ids, region, now)
        violations += self._review_violations(candidate["candidate_id"], region)
        violations += self._standards_violations(component_ids, region, now)
        return violations

    def _add_event(
        self, release_id: str, event: str, reason: str, now: float
    ) -> None:
        self.storage.write(
            "INSERT INTO release_events(event_id, release_id, event, reason,"
            " created_at) VALUES(?,?,?,?,?)",
            (self.ids.new_id("evt"), release_id, event, reason, now),
        )

    def _refresh_risks(self, now: float) -> list[str]:
        """把因授权撤回/到期而不再满足约束的已发布版本标记为 at_risk。

        只标记、不删除；at_risk 是粘性状态，需运营回滚或修复后重新签发。
        返回本次被标记的版本 ID 列表。须在事务内调用。
        """
        affected: list[str] = []
        for row in self.storage.all("SELECT * FROM releases WHERE status='published'"):
            candidate = self._get_candidate(row["candidate_id"])
            violations = self._license_dep_violations(
                candidate["component_ids"], row["region"], now
            )
            if not violations:
                continue
            self.storage.write(
                "UPDATE releases SET status='at_risk', risk_reason=?"
                " WHERE release_id=?",
                (json.dumps(violations, ensure_ascii=False), row["release_id"]),
            )
            self._add_event(
                row["release_id"],
                "at_risk",
                f"授权状态变化，{len(violations)} 项约束失效",
                now,
            )
            affected.append(row["release_id"])
        return affected
