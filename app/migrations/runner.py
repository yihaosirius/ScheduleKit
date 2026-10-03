"""迁移执行器。

为什么不用 Alembic：这是单机单文件 SQLite，schema 变更靠手写 SQL 更快也更可控；
引入 Alembic 只会多一层需要理解的抽象。

约定：

* 文件名 ``NNNN_描述.sql``，``NNNN`` 为四位递增序号，**一旦提交就不再修改**。
* 每个文件在**一个事务**里执行（SQLite 的 DDL 是事务性的，这点比 MySQL 好）。
* 执行记录写进 ``schema_version``；已应用过的文件不再重复执行，
  所以迁移必须是"从空库开始按序执行"的语义，而不是幂等 SQL。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app.logging import get_logger, kv

log = get_logger("migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent
_NAME_RE = re.compile(r"^(\d{4})_.+\.sql$")

_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def discover(directory: Path | None = None) -> list[tuple[int, Path]]:
    """按序号返回待应用的迁移文件。序号重复直接报错（否则执行顺序不确定）。"""
    base = directory or MIGRATIONS_DIR
    found: list[tuple[int, Path]] = []
    for path in sorted(base.glob("*.sql")):
        match = _NAME_RE.match(path.name)
        if not match:
            log.warning("migration.name_ignored %s", kv(file=path.name))
            continue
        found.append((int(match.group(1)), path))

    seen: dict[int, str] = {}
    for version, path in found:
        if version in seen:
            raise RuntimeError(
                f"迁移序号重复：{version:04d} 同时出现在 {seen[version]} 与 {path.name}"
            )
        seen[version] = path.name
    return found


def applied_versions(con: sqlite3.Connection) -> set[int]:
    con.execute(_TABLE)
    return {row["version"] for row in con.execute("SELECT version FROM schema_version")}


def split_statements(sql: str) -> list[str]:
    """把脚本切成单条语句。

    **不能用 ``Connection.executescript``**：它在执行前会隐式 COMMIT
    （CPython 的 sqlite3 实现如此），于是我们外面那层 ``BEGIN IMMEDIATE``
    会被提前提交，迁移一旦中途失败就留下半个 schema，而且
    ``ROLLBACK`` 也没得可回。既然要事务性，就只能自己切语句。

    用 ``sqlite3.complete_statement`` 判断语句边界，而不是简单 ``split(';')``：
    分号可能出现在字符串字面量或注释里。
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            stripped = buffer.strip()
            if stripped:
                statements.append(stripped)
            buffer = ""
    tail = buffer.strip()
    if tail:
        raise RuntimeError(
            "迁移脚本末尾有未闭合的语句（缺少结尾分号）：" + tail[-80:]
        )
    return statements


def run(con: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """应用所有未执行的迁移，返回本次执行的序号列表。"""
    from app.timeutil import now_utc_iso

    done = applied_versions(con)
    executed: list[int] = []
    for version, path in discover(directory):
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        statements = split_statements(sql)
        log.info(
            "migration.apply %s",
            kv(version=version, file=path.name, statements=len(statements)),
        )
        con.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                con.execute(statement)
            con.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (version, path.name, now_utc_iso()),
            )
        except BaseException:
            con.execute("ROLLBACK")
            log.error("migration.failed %s", kv(version=version, file=path.name))
            raise
        con.execute("COMMIT")
        executed.append(version)
    if executed:
        log.info("migration.done %s", kv(applied=len(executed), versions=executed))
    return executed
