"""SQLite 持久化：模式、线程本地连接与写事务。

全部运行状态集中在单个 SQLite 文件中，服务重启后重新打开即可恢复。
写操作经进程内写锁串行化，并以 BEGIN IMMEDIATE 取得数据库写锁；
唯一部分索引保证同一课程/地区/标准最多一个进行中的发布。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS components (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    author      TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS variants (
    id           TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(id),
    language     TEXT NOT NULL,
    standards    TEXT NOT NULL DEFAULT '[]',
    content      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    created_by   TEXT,
    created_at   REAL NOT NULL,
    UNIQUE (component_id, language, fingerprint)
);

CREATE TABLE IF NOT EXISTS licenses (
    id              TEXT PRIMARY KEY,
    component_id    TEXT NOT NULL REFERENCES components(id),
    licensor        TEXT,
    regions         TEXT NOT NULL DEFAULT '[]',
    standards       TEXT NOT NULL DEFAULT '[]',
    valid_from      REAL,
    valid_until     REAL,
    status          TEXT NOT NULL DEFAULT 'active',
    withdrawn_at    REAL,
    withdraw_reason TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dependencies (
    component_id TEXT NOT NULL REFERENCES components(id),
    depends_on   TEXT NOT NULL REFERENCES components(id),
    note         TEXT,
    PRIMARY KEY (component_id, depends_on)
);

CREATE TABLE IF NOT EXISTS candidates (
    id                 TEXT PRIMARY KEY,
    course_id          TEXT NOT NULL,
    title              TEXT,
    target_region      TEXT NOT NULL,
    target_standard    TEXT NOT NULL,
    required_approvals INTEGER NOT NULL DEFAULT 2,
    status             TEXT NOT NULL DEFAULT 'open',
    created_by         TEXT,
    created_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_selections (
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    component_id TEXT NOT NULL,
    variant_id   TEXT NOT NULL REFERENCES variants(id),
    PRIMARY KEY (candidate_id, component_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id           TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    reviewer     TEXT NOT NULL,
    decision     TEXT NOT NULL,
    comment      TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id              TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL REFERENCES candidates(id),
    course_id       TEXT NOT NULL,
    target_region   TEXT NOT NULL,
    target_standard TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    snapshot        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    risk_status     TEXT NOT NULL DEFAULT 'ok',
    risk_reasons    TEXT NOT NULL DEFAULT '[]',
    issued_by       TEXT,
    issued_at       REAL NOT NULL,
    rolled_back_at  REAL,
    rollback_reason TEXT
);

-- 同一课程在同一地区/标准下最多一个进行中的发布（并发签发的最后防线）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_release
    ON releases (course_id, target_region, target_standard)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_variants_component ON variants(component_id);
CREATE INDEX IF NOT EXISTS idx_licenses_component ON licenses(component_id);
CREATE INDEX IF NOT EXISTS idx_reviews_candidate ON reviews(candidate_id);
CREATE INDEX IF NOT EXISTS idx_releases_candidate ON releases(candidate_id);
"""


class Database:
    """线程安全的 SQLite 访问层：每线程一个连接，写操作互斥。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        # 自动提交模式下建表（executescript 会隐式提交，不能放在写事务里）。
        self.conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的连接（惰性建立）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def write_txn(self) -> Iterator[sqlite3.Connection]:
        """写事务：进程内互斥 + BEGIN IMMEDIATE，异常时回滚。可嵌套（并入外层）。"""
        with self._write_lock:
            conn = self.conn
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        """关闭当前线程的连接（重启恢复测试据此验证状态已落盘）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
