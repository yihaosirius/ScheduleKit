"""命令行入口：部署脚本与运维用。

设计取舍：

* **所有输出都是 JSON**（``--pretty`` 可选）。上一版还要人肉解析文本输出，
  部署脚本只能靠 ``grep``，那是最脆的一类集成方式。
* ``show --json --secrets`` 才输出密钥，仅部署脚本（root）用；
  默认输出里密钥一律脱敏。
* 交互式设置密码用 ``getpass``，不带 ``--password`` 参数以避免密码进
  shell history。测试需要非交互时用环境变量 ``SK_NEW_PASSWORD``。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from app.config import Config, ConfigError, load_config, resolve_config_path, update_config_section
from app.db import connect, transaction
from app.logging import get_logger, setup_logging
from app.security import generate_api_key, hash_password
from app.timeutil import now_utc_iso

log = get_logger("cli")

_SECRET_FIELDS = {
    "auth": ("secret_key", "password_hash"),
    "llm": ("api_key",),
    "tls": ("duckdns_token",),
}


def _load(args: argparse.Namespace) -> Config:
    return load_config(getattr(args, "config", None))


def _emit(payload: dict[str, Any], pretty: bool) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    sys.stdout.write(text + "\n")


def _apply_migrations(cfg: Config) -> list[int]:
    from app.migrations import runner
    from app.paths import ensure_dirs

    ensure_dirs(cfg)
    con = connect(cfg.db_path)
    try:
        return runner.run(con)
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #
def _masked_config(cfg: Config, *, include_secrets: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "config_path": str(cfg.path),
        "server": {
            "public_url": cfg.server.public_url,
            "timezone": cfg.server.timezone,
            "listen_host": cfg.server.listen_host,
            "listen_port": cfg.server.listen_port,
            "data_dir": str(cfg.data_dir),
        },
        "term": {"start_date": cfg.term.start_date, "total_weeks": cfg.term.total_weeks},
        "llm": {
            "provider": cfg.llm.provider,
            "base_url": cfg.llm.base_url,
            "model": cfg.llm.model,
            "thinking": cfg.llm.thinking,
            "api_key": cfg.llm.api_key,
        },
        "ingest": {
            "confirm_ttl_hours": cfg.ingest.confirm_ttl_hours,
            "max_items": cfg.ingest.max_items,
            "memory_char_budget": cfg.ingest.memory_char_budget,
        },
        "tls": {
            "provider": cfg.tls.provider,
            "domain": cfg.tls.domain,
            "port": cfg.tls.port,
            "acme_email": cfg.tls.acme_email,
            "cert_dir": cfg.tls.cert_dir,
            "duckdns_token": cfg.tls.duckdns_token,
        },
        "auth": {"secret_key": cfg.auth.secret_key, "session_epoch": cfg.auth.session_epoch},
        "backup": {
            "enabled": cfg.backup.enabled,
            "hour": cfg.backup.hour,
            "keep": cfg.backup.keep,
            "upload_retention_days": cfg.backup.upload_retention_days,
        },
    }
    if include_secrets:
        return payload

    for section, fields in _SECRET_FIELDS.items():
        for field in fields:
            value = payload[section].get(field)
            payload[section][field] = (
                "" if not value else ("***" if len(str(value)) < 8 else f"{str(value)[:4]}***")
            )
    return payload


def cmd_show(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _emit(_masked_config(cfg, include_secrets=args.secrets), args.pretty)
    return 0


# --------------------------------------------------------------------------- #
# init / migrate
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> int:
    path = resolve_config_path(getattr(args, "config", None))
    if not path.exists():
        raise ConfigError(
            f"配置文件不存在：{path}\n"
            "先 `cp config.toml.example config.toml`（生产环境见 deploy/install.sh）。"
        )

    updates: dict[str, dict[str, Any]] = {}
    cfg = load_config(path)
    if not cfg.auth.secret_key:
        updates.setdefault("auth", {})["secret_key"] = secrets.token_urlsafe(48)

    password_plain = _read_password(args)
    if password_plain:
        updates.setdefault("auth", {})["password_hash"] = hash_password(password_plain)

    if updates:
        for section, values in updates.items():
            update_config_section(path, section, values)

    cfg = load_config(path)
    applied = _apply_migrations(cfg)
    _emit(
        {
            "ok": True,
            "config_path": str(path),
            "data_dir": str(cfg.data_dir),
            "db_path": str(cfg.db_path),
            "generated": sorted(section for section in updates),
            "migrations_applied": applied,
            "next": "uv run python -m app.serve",
        },
        args.pretty,
    )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _emit({"ok": True, "migrations_applied": _apply_migrations(cfg)}, args.pretty)
    return 0


# --------------------------------------------------------------------------- #
# set-password
# --------------------------------------------------------------------------- #
def _read_password(args: argparse.Namespace) -> str | None:
    """非交互优先取 ``SK_NEW_PASSWORD``（测试用），否则交互式询问。"""
    if getattr(args, "no_password", False):
        return None
    from_env = os.environ.get("SK_NEW_PASSWORD")
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        return None
    first = getpass.getpass("新密码：")
    if not first:
        raise ConfigError("密码不能为空")
    second = getpass.getpass("再输一次：")
    if first != second:
        raise ConfigError("两次输入不一致")
    return first


def cmd_set_password(args: argparse.Namespace) -> int:
    cfg = _load(args)
    plain = _read_password(args)
    if not plain:
        raise ConfigError(
            "没有拿到新密码。交互终端下会提示输入；非交互环境请设置 SK_NEW_PASSWORD。"
        )

    update_config_section(
        cfg.path,
        "auth",
        {
            "password_hash": hash_password(plain),
            # epoch 自增让所有既有会话立即失效——无状态 Cookie 唯一的吊销手段
            "session_epoch": cfg.auth.session_epoch + 1,
        },
    )
    _emit(
        {
            "ok": True,
            "config_path": str(cfg.path),
            "session_epoch": cfg.auth.session_epoch + 1,
            "note": "所有既有登录会话已失效",
        },
        args.pretty,
    )
    return 0


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
def cmd_new_key(args: argparse.Namespace) -> int:
    cfg = _load(args)
    plain, digest, prefix = generate_api_key()
    con = connect(cfg.db_path)
    try:
        with transaction(con):
            cursor = con.execute(
                "INSERT INTO api_keys (name, key_hash, scope, prefix, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (args.name, digest, "read" if args.read_only else "write", prefix, now_utc_iso()),
            )
            key_id = cursor.lastrowid
    finally:
        con.close()

    _emit(
        {
            "ok": True,
            "id": key_id,
            "name": args.name,
            "scope": "read" if args.read_only else "write",
            # 明文只在这里出现一次
            "api_key": plain,
            "note": "明文只显示这一次；服务端只保留 sha256",
        },
        args.pretty,
    )
    return 0


def cmd_list_keys(args: argparse.Namespace) -> int:
    cfg = _load(args)
    con = connect(cfg.db_path)
    try:
        rows = con.execute(
            "SELECT id, name, scope, prefix, created_at, last_used_at, revoked_at"
            " FROM api_keys ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    _emit({"ok": True, "keys": [dict(row) for row in rows]}, args.pretty)
    return 0


def cmd_revoke_key(args: argparse.Namespace) -> int:
    cfg = _load(args)
    con = connect(cfg.db_path)
    try:
        with transaction(con):
            cursor = con.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now_utc_iso(), args.id),
            )
            changed = cursor.rowcount
    finally:
        con.close()
    if not changed:
        raise ConfigError(f"没有找到未吊销的 key：id={args.id}")
    _emit({"ok": True, "id": args.id, "revoked": True}, args.pretty)
    return 0


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #
def cmd_backup(args: argparse.Namespace) -> int:
    """用 sqlite3 的在线 backup API，而不是拷文件。

    WAL 模式下直接 ``cp`` 可能拿到"已提交但还在 WAL 里"的数据缺失副本；
    backup API 会先起一个一致性快照。备份完顺手 VACUUM INTO 一份紧凑副本
    也可以，但这里保持简单：只做一致性快照。
    """
    import sqlite3

    cfg = _load(args)
    from app.paths import ensure_dirs

    ensure_dirs(cfg)
    stamp = now_utc_iso().replace(":", "").replace("-", "")[:15]
    target = cfg.backups_dir / f"schedulekit-{stamp}.db"

    source = sqlite3.connect(str(cfg.db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    removed = _prune_backups(cfg)
    _emit(
        {"ok": True, "path": str(target), "bytes": target.stat().st_size,
         "pruned": removed, "keep": cfg.backup.keep},
        args.pretty,
    )
    return 0


def _prune_backups(cfg: Config) -> list[str]:
    keep = max(1, cfg.backup.keep)
    files = sorted(cfg.backups_dir.glob("schedulekit-*.db"), key=lambda p: p.name)
    removed: list[str] = []
    for stale in files[:-keep]:
        try:
            stale.unlink()
            removed.append(stale.name)
        except OSError as exc:  # pragma: no cover
            log.warning("backup.prune_failed %s", exc)
    return removed


def cmd_check(args: argparse.Namespace) -> int:
    """启动前自检。给部署脚本用，避免"起来了但一调用就炸"。"""
    cfg = _load(args)
    cfg.validate_for_startup()
    problems: list[str] = []
    if not cfg.db_path.exists():
        problems.append(f"数据库不存在：{cfg.db_path}（先跑 `app.cli init`）")
    if cfg.llm.provider != "mock":
        try:
            cfg.validate_for_llm()
        except ConfigError as exc:
            problems.append(str(exc))
    _emit(
        {
            "ok": not problems,
            "config_path": str(cfg.path),
            "data_dir": str(cfg.data_dir),
            "timezone": cfg.server.timezone,
            "provider": cfg.llm.provider,
            "problems": problems,
        },
        args.pretty,
    )
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="ScheduleKit 命令行")
    parser.add_argument("--config", help="配置文件路径（默认 SK_CONFIG 或 /etc/schedulekit/config.toml）")
    parser.add_argument("--pretty", action="store_true", help="美化 JSON 输出")
    parser.add_argument("--verbose", action="store_true", help="打开调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="输出配置 JSON（默认脱敏）")
    show.add_argument("--json", action="store_true", help="显式要求 JSON（默认就是）")
    show.add_argument("--secrets", action="store_true", help="包含密钥，仅供 root 部署脚本")
    show.set_defaults(func=cmd_show)

    init = sub.add_parser("init", help="生成 secret_key、设置密码、建库")
    init.add_argument("--no-password", action="store_true", help="不设置密码")
    init.set_defaults(func=cmd_init)

    sub.add_parser("migrate", help="应用数据库迁移").set_defaults(func=cmd_migrate)

    setpw = sub.add_parser("set-password", help="修改管理员密码（旧会话全部失效）")
    setpw.set_defaults(func=cmd_set_password)

    newkey = sub.add_parser("new-key", help="创建 API Key")
    newkey.add_argument("name", help="用途备注，如 iphone-shortcut")
    newkey.add_argument("--read-only", action="store_true", help="只读 scope")
    newkey.set_defaults(func=cmd_new_key)

    sub.add_parser("list-keys", help="列出 API Key（不含明文）").set_defaults(func=cmd_list_keys)

    revoke = sub.add_parser("revoke-key", help="吊销 API Key")
    revoke.add_argument("id", type=int)
    revoke.set_defaults(func=cmd_revoke_key)

    sub.add_parser("backup", help="一致性备份数据库并按 keep 清理").set_defaults(func=cmd_backup)
    sub.add_parser("check", help="启动前自检").set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")
    try:
        return int(args.func(args))
    except ConfigError as exc:
        # 配置类错误直接给人看，不打堆栈——堆栈只会淹没真正的原因
        sys.stderr.write(f"配置错误：{exc}\n")
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
