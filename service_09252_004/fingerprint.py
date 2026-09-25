"""内容指纹：对规范化 JSON 取 SHA-256，用于幂等写入与跨地区去重。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """生成与键序、空白无关的规范化 JSON 文本。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_fingerprint(value: Any) -> str:
    """返回 ``sha256:<hex>`` 形式的内容指纹。"""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
