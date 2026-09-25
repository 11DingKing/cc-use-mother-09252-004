#!/usr/bin/env python3
"""服务入口：数字教学资源联合发布。

运行数据默认写入工作目录下的 var/（不进入源码包），
也可用 --db 或环境变量 RELEASE_DB 指定其他位置。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from service_09252_004.api import serve
from service_09252_004.service import ReleaseService


def main() -> None:
    parser = argparse.ArgumentParser(description="数字教学资源联合发布服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--db",
        default=os.environ.get("RELEASE_DB", str(Path.cwd() / "var" / "releases.db")),
        help="SQLite 数据文件路径（重启后据此恢复全部状态）",
    )
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    service = ReleaseService(args.db)
    print(f"listening on http://{args.host}:{args.port} (db: {args.db})")
    try:
        serve(service, args.host, args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
