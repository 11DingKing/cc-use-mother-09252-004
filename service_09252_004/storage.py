"""SQLite 持久化层：连接管理、模式迁移与事务边界。

单连接 + 可重入锁，所有写操作必须在 ``transaction()``（BEGIN IMMEDIATE）内执行，
因此多线程并发写入被串行化，配合唯一约束保证幂等（如同一候选+地区只签发一次）。
数据库文件可安全关闭后重新打开，实现重启恢复。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
-- 资源组件：原稿，内容指纹唯一，上传后不可变（不覆盖各方原稿）
CREATE TABLE IF NOT EXISTS components (
    component_id  TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    author        TEXT NOT NULL,
    origin_region TEXT NOT NULL,
    payload       TEXT NOT NULL,          -- 元数据 JSON
    created_at    REAL NOT NULL
);

-- 语言变体：某组件在特定语言/地区下的字幕、案例等本地化内容
CREATE TABLE IF NOT EXISTS variants (
    variant_id   TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    language     TEXT NOT NULL,
    region       TEXT NOT NULL,
    subtitles    TEXT NOT NULL,           -- JSON 数组：字幕语言
    cases        TEXT NOT NULL,           -- JSON 数组：本地化案例标识
    payload      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL UNIQUE,
    created_at   REAL NOT NULL
);

-- 版权授权：按组件授予，限定地区与适用标准；撤回只改状态，不删行
CREATE TABLE IF NOT EXISTS licenses (
    license_id   TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    licensor     TEXT NOT NULL,
    regions      TEXT NOT NULL,           -- JSON 数组：授权覆盖地区
    standards    TEXT NOT NULL,           -- JSON 数组：满足的适用标准
    valid_from   REAL,
    valid_until  REAL,                    -- NULL 表示长期有效
    status       TEXT NOT NULL DEFAULT 'active',  -- active | withdrawn
    withdrawn_at REAL,
    created_at   REAL NOT NULL
);

-- 组件依赖：发布时依赖方组件同样须满足目标地区约束
CREATE TABLE IF NOT EXISTS dependencies (
    component_id         TEXT NOT NULL REFERENCES components(component_id),
    depends_on_id        TEXT NOT NULL REFERENCES components(component_id),
    required_fingerprint TEXT,            -- 可选：钉扎依赖组件的内容指纹
    created_at           REAL NOT NULL,
    PRIMARY KEY (component_id, depends_on_id)
);

-- 目标地区约束：必须覆盖的标准与最少评审通过人数
CREATE TABLE IF NOT EXISTS region_policies (
    region             TEXT PRIMARY KEY,
    required_standards TEXT NOT NULL,     -- JSON 数组
    required_approvals INTEGER NOT NULL,
    updated_at         REAL NOT NULL
);

-- 合并候选：一组变体的组合，仅引用原稿；指纹 = 地区 + 变体集合，幂等
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    region       TEXT NOT NULL,
    fingerprint  TEXT NOT NULL UNIQUE,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_items (
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    variant_id   TEXT NOT NULL REFERENCES variants(variant_id),
    PRIMARY KEY (candidate_id, variant_id)
);

-- 审核意见：追加式，支持并行评审；同一评审人同一范围以最新一条为准
CREATE TABLE IF NOT EXISTS reviews (
    review_id    TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    reviewer     TEXT NOT NULL,
    scope        TEXT NOT NULL,           -- 评审范围，默认候选目标地区
    decision     TEXT NOT NULL,           -- approve | reject
    comment      TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL
);

-- 发布版本：published | at_risk | rolled_back；历史一律保留
CREATE TABLE IF NOT EXISTS releases (
    release_id    TEXT PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates(candidate_id),
    region        TEXT NOT NULL,
    status        TEXT NOT NULL,
    signed_at     REAL NOT NULL,
    rolled_back_at REAL,
    risk_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_releases_candidate ON releases(candidate_id, region, status);

-- 发布版本状态变迁事件：signed | at_risk | rolled_back
CREATE TABLE IF NOT EXISTS release_events (
    event_id   TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    event      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


class Storage:
    """SQLite 存储门面。线程安全；写路径一律走事务。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务（BEGIN IMMEDIATE）。嵌套调用并入外层事务；异常时整体回滚。"""
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """执行写语句；必须在 transaction() 块内调用。"""
        with self._lock:
            if not self._conn.in_transaction:
                raise RuntimeError("write outside of transaction")
            return self._conn.execute(sql, params)
