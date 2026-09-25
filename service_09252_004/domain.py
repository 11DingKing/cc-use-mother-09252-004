"""领域模型与内容指纹。

多国教师共同制作课程时，资源组件(component)由各方分别维护，
语言变体(variant)承载字幕、案例等具体内容，只增不改；
合并候选(candidate)通过引用组合变体，从不覆盖各方原稿；
发布(release)是候选通过目标地区全部约束后的签发快照，
历史版本（含被取代、被回滚的）一律保留。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

# ---- 授权状态（库内）与派生状态 ----
LICENSE_ACTIVE = "active"
LICENSE_WITHDRAWN = "withdrawn"
LICENSE_EXPIRED = "expired"  # 派生：valid_until 已过
LICENSE_NOT_YET_VALID = "not_yet_valid"  # 派生：valid_from 未到

# ---- 发布状态 ----
RELEASE_ACTIVE = "active"
RELEASE_SUPERSEDED = "superseded"
RELEASE_ROLLED_BACK = "rolled_back"

# ---- 风险标记 ----
RISK_OK = "ok"
RISK_AT_RISK = "at_risk"
REASON_LICENSE_WITHDRAWN = "license_withdrawn"
REASON_LICENSE_EXPIRED = "license_expired"

# ---- 评审决定 ----
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"

# ---- 候选状态 ----
CANDIDATE_OPEN = "open"
CANDIDATE_PUBLISHED = "published"


def canonical_fingerprint(payload: dict[str, Any]) -> str:
    """规范化 JSON 序列化后取 SHA-256，作为内容指纹。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def variant_fingerprint(language: str, standards: list[str], content: Any) -> str:
    """变体内容指纹：同一组件同一语言下内容相同则指纹相同，用于去重与幂等。"""
    return canonical_fingerprint(
        {"language": language, "standards": sorted(standards), "content": content}
    )


def release_fingerprint(
    course_id: str, region: str, standard: str, selections: dict[str, str]
) -> str:
    """发布指纹：课程 + 目标地区 + 适用标准 + 各组件选中变体的内容指纹。"""
    return canonical_fingerprint(
        {
            "course_id": course_id,
            "region": region,
            "standard": standard,
            "selections": [
                {"component_id": comp, "variant": fp}
                for comp, fp in sorted(selections.items())
            ],
        }
    )


@dataclass
class _Model:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Component(_Model):
    id: str
    name: str
    kind: str
    author: str | None
    metadata: dict[str, Any]
    created_at: float

    @classmethod
    def from_row(cls, row: Any) -> "Component":
        return cls(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            author=row["author"],
            metadata=json.loads(row["metadata"]),
            created_at=row["created_at"],
        )


@dataclass
class Variant(_Model):
    id: str
    component_id: str
    language: str
    standards: list[str]
    content: Any
    fingerprint: str
    metadata: dict[str, Any]
    created_by: str | None
    created_at: float

    @classmethod
    def from_row(cls, row: Any) -> "Variant":
        return cls(
            id=row["id"],
            component_id=row["component_id"],
            language=row["language"],
            standards=json.loads(row["standards"]),
            content=json.loads(row["content"]),
            fingerprint=row["fingerprint"],
            metadata=json.loads(row["metadata"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
        )


@dataclass
class License(_Model):
    id: str
    component_id: str
    licensor: str | None
    regions: list[str]
    standards: list[str]
    valid_from: float | None
    valid_until: float | None
    status: str
    withdrawn_at: float | None
    withdraw_reason: str | None
    created_at: float

    @classmethod
    def from_row(cls, row: Any) -> "License":
        return cls(
            id=row["id"],
            component_id=row["component_id"],
            licensor=row["licensor"],
            regions=json.loads(row["regions"]),
            standards=json.loads(row["standards"]),
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            status=row["status"],
            withdrawn_at=row["withdrawn_at"],
            withdraw_reason=row["withdraw_reason"],
            created_at=row["created_at"],
        )


@dataclass
class Candidate(_Model):
    id: str
    course_id: str
    title: str | None
    target_region: str
    target_standard: str
    required_approvals: int
    status: str
    created_by: str | None
    created_at: float
    selections: dict[str, str] = field(default_factory=dict)  # component_id -> variant_id

    @classmethod
    def from_row(cls, row: Any, selections: dict[str, str] | None = None) -> "Candidate":
        return cls(
            id=row["id"],
            course_id=row["course_id"],
            title=row["title"],
            target_region=row["target_region"],
            target_standard=row["target_standard"],
            required_approvals=row["required_approvals"],
            status=row["status"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            selections=selections or {},
        )


@dataclass
class Review(_Model):
    id: str
    candidate_id: str
    reviewer: str
    decision: str
    comment: str | None
    created_at: float

    @classmethod
    def from_row(cls, row: Any) -> "Review":
        return cls(
            id=row["id"],
            candidate_id=row["candidate_id"],
            reviewer=row["reviewer"],
            decision=row["decision"],
            comment=row["comment"],
            created_at=row["created_at"],
        )


@dataclass
class Release(_Model):
    id: str
    candidate_id: str
    course_id: str
    target_region: str
    target_standard: str
    fingerprint: str
    snapshot: dict[str, Any]
    status: str
    risk_status: str
    risk_reasons: list[dict[str, Any]]
    issued_by: str | None
    issued_at: float
    rolled_back_at: float | None
    rollback_reason: str | None

    @classmethod
    def from_row(cls, row: Any) -> "Release":
        return cls(
            id=row["id"],
            candidate_id=row["candidate_id"],
            course_id=row["course_id"],
            target_region=row["target_region"],
            target_standard=row["target_standard"],
            fingerprint=row["fingerprint"],
            snapshot=json.loads(row["snapshot"]),
            status=row["status"],
            risk_status=row["risk_status"],
            risk_reasons=json.loads(row["risk_reasons"]),
            issued_by=row["issued_by"],
            issued_at=row["issued_at"],
            rolled_back_at=row["rolled_back_at"],
            rollback_reason=row["rollback_reason"],
        )
