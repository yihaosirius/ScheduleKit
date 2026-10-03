"""SQLite 连接与事务。

PRAGMA 选择理由：

* ``journal_mode=WAL`` —— 读写不互相阻塞。控制台读状态时不会卡住快捷指令写入。
* ``synchronous=NORMAL`` —— WAL 下的常规取舍：掉电可能丢最后一个事务，
  但不会损坏库。这台机器只有 1.6G 内存，FULL 会明显增加 fsync 压力。
* ``foreign_keys=ON`` —— SQLite 默认**关**外键，不显式打开的话
  ``course_sessions.course_id`` 的约束形同虚设。
* ``busy_timeout=5000`` —— 并发写时宁可等 5 秒也不要立刻抛 SQLITE_BUSY。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.logging import get_logger

log = get_logger("db")

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接。``check_same_thread=False`` 是给 FastAPI 线程池用的。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        con.execute(pragma)
    return con


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务。``isolation_level=None`` 下必须自己 BEGIN/COMMIT。

    确认草稿要"插任务 + 标草稿已确认"两件事一起成立，只有包在事务里才安全。
    用 ``IMMEDIATE`` 而不是默认的 ``DEFERRED``：并发时会立刻拿写锁，
    避免升级锁失败导致的 SQLITE_BUSY（那种失败在提交时才报，很难处理）。
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def close_quietly(con: sqlite3.Connection | None) -> None:
    if con is None:
        return
    try:
        con.close()
    except sqlite3.Error as exc:  # pragma: no cover - 关闭失败不影响业务
        log.warning("db.close_failed %s", exc)
