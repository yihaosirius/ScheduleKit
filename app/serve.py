"""服务入口：``python -m app.serve``。

systemd 单元直接跑这个模块（见 ``deploy/schedulekit.service``）。

两个刻意的选择：

* **不用 ``uvicorn.main()`` 的 CLI**，而是显式调 ``uvicorn.run``：CLI 会自己
  ``load()`` 配置，于是"配置从哪来"变成两套逻辑。这里保证配置只解析一次。
* 传 ``factory="app.main:create_app"`` 而不是应用实例：``--reload`` 时 uvicorn
  在子进程里重新导入，"每次重载都读一次最新配置"正是我们想要的。
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from app.config import ConfigError, load_config, resolve_config_path
from app.logging import get_logger, setup_logging

log = get_logger("serve")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.serve", description="启动 ScheduleKit")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--host", help="覆盖监听地址（默认取配置，且必须是回环）")
    parser.add_argument("--port", type=int, help="覆盖监听端口")
    parser.add_argument("--reload", action="store_true", help="开发用：改动自动重载")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别")
    args = parser.parse_args(argv)

    setup_logging("DEBUG" if args.log_level.lower() == "debug" else "INFO")

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        sys.stderr.write(f"启动失败：{exc}\n")
        return 2

    host = args.host or cfg.server.listen_host
    port = args.port or cfg.server.listen_port

    log.info(
        "serve.start %s",
        kv(config=str(resolve_config_path(args.config)), host=host, port=port, reload=args.reload),
    )

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        # 自动重载只盯 Python 源码，别去扫 data/（截图落盘会触发无意义的重启）
        reload_dirs=["app"] if args.reload else None,
        log_level=args.log_level,
        # 访问日志交给我们自己的中间件（带 trace id），关掉 uvicorn 的重复记录
        access_log=False,
        # 反代后面拿到的是 X-Real-IP，我们自己在 deps.client_ip 处理
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        server_header=False,
        date_header=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
