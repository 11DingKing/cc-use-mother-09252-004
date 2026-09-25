"""服务入口：``python3 -m service_09252_004``。

运行数据（SQLite 数据库）默认放在用户数据目录而非源码目录，
可用环境变量覆盖：COURSEHUB_DB / COURSEHUB_HOST / COURSEHUB_PORT。
"""
from __future__ import annotations

import os
from pathlib import Path

from .api import make_server
from .service import CourseHubService


def default_db_path() -> Path:
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "service_09252_004" / "coursehub.db"


def main() -> None:
    db_path = os.environ.get("COURSEHUB_DB") or str(default_db_path())
    host = os.environ.get("COURSEHUB_HOST", "127.0.0.1")
    port = int(os.environ.get("COURSEHUB_PORT", "8080"))
    service = CourseHubService(db_path)
    server = make_server(service, host, port)
    actual_port = server.server_address[1]
    print(f"coursehub listening on http://{host}:{actual_port} (db: {db_path})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
