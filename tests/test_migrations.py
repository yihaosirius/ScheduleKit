"""迁移与 schema 约束测试。

重点不在"表建出来了"，而在**库层是否真的挡得住坏数据**：
二选一约束、外键级联、索引是否存在。服务层当然也会校验，
但手工改库、迁移脚本、未来的批处理都可能绕过服务层——那时只剩 CHECK 兜底。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import connect, transaction
from app.migrations import runner

EXPECTED_TABLES = {
    "schema_version",
    "items",
    "memories",
    "courses",
    "course_sessions",
    "ingest_drafts",
    "api_keys",
}


def _fresh(work_dir: Path) -> sqlite3.Connection:
    con = connect(work_dir / "test.db")
    runner.run(con)
    return con


def test_migrations_create_all_tables(work_dir: Path) -> None:
    con = _fresh(work_dir)
    tables = {
        row["name"]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert EXPECTED_TABLES <= tables


def test_migrations_are_idempotent(work_dir: Path) -> None:
    con = _fresh(work_dir)
    assert runner.run(con) == []
    assert runner.run(con) == []


def test_migration_order_is_sequential(work_dir: Path) -> None:
    versions = [version for version, _ in runner.discover()]
    assert versions == sorted(versions)
    assert versions, "没有发现任何迁移文件"


def test_duplicate_migration_number_is_rejected(work_dir: Path) -> None:
    """序号重复时执行顺序不确定，必须显式报错而不是碰运气。"""
    (work_dir / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (work_dir / "0001_b.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(RuntimeError, match="重复"):
        runner.discover(work_dir)


def test_failed_migration_rolls_back_whole_file(work_dir: Path) -> None:
    """一个文件里第 N 条语句失败时，前面建的表必须一起回滚。

    这正是**不能用 executescript** 的原因：它会在执行前隐式 COMMIT，
    于是回滚无从谈起。
    """
    directory = work_dir / "migrations"
    directory.mkdir()
    (directory / "0001_partial.sql").write_text(
        "CREATE TABLE good (id INTEGER);\n"
        "CREATE TABLE bad (id INTEGER, CHECK (nope = 1));\n",
        encoding="utf-8",
    )
    con = connect(work_dir / "partial.db")
    with pytest.raises(sqlite3.Error):
        runner.run(con, directory)

    tables = {
        row["name"]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "good" not in tables, "失败的迁移留下了半截 schema"
    assert runner.applied_versions(con) == set()


def test_unterminated_statement_is_rejected(work_dir: Path) -> None:
    with pytest.raises(RuntimeError, match="未闭合"):
        runner.split_statements("CREATE TABLE x (id INTEGER)")


def test_split_statements_ignores_semicolons_in_strings() -> None:
    sql = "INSERT INTO t (v) VALUES ('a;b');\nINSERT INTO t (v) VALUES ('c');\n"
    assert len(runner.split_statements(sql)) == 2


# --------------------------------------------------------------------------- #
# items：二选一约束
# --------------------------------------------------------------------------- #
ITEM_COLUMNS = (
    "title, category, due_at, priority, status, source, memory_ids, created_at, updated_at"
)
ITEM_VALUES = ("?, ?, ?, ?, ?, ?, '[]', '2026-10-03T00:00:00+00:00', '2026-10-03T00:00:00+00:00'")


def _insert_item(
    con: sqlite3.Connection,
    *,
    title: str = "任务",
    category: str = "homework",
    due_at: str | None = None,
    priority: int | None = None,
    status: str = "open",
    source: str = "web",
) -> None:
    con.execute(
        f"INSERT INTO items ({ITEM_COLUMNS}) VALUES ({ITEM_VALUES})",
        (title, category, due_at, priority, status, source),
    )


def test_item_requires_exactly_one_of_due_at_priority(work_dir: Path) -> None:
    con = _fresh(work_dir)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(con, due_at=None, priority=None)  # 两个都空 → 无序表里也没档位

    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(
            con,
            due_at="2026-10-10T15:59:00+00:00",
            priority=2,  # 两个都给 → 该进哪张表？
        )


def test_item_accepts_each_alternative(work_dir: Path) -> None:
    con = _fresh(work_dir)
    _insert_item(con, title="有截止", due_at="2026-10-10T15:59:00+00:00")
    _insert_item(con, title="有档位", priority=3)
    assert con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 2


@pytest.mark.parametrize("priority", [0, 6, -1])
def test_item_priority_range(work_dir: Path, priority: int) -> None:
    con = _fresh(work_dir)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(con, priority=priority)


@pytest.mark.parametrize("category", ["", "HW", "homework "])
def test_item_category_enum(work_dir: Path, category: str) -> None:
    con = _fresh(work_dir)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(con, category=category, priority=1)


def test_item_status_and_source_enums(work_dir: Path) -> None:
    con = _fresh(work_dir)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(con, status="finished", priority=1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(con, source="telegram", priority=1)


def test_item_client_uuid_is_unique(work_dir: Path) -> None:
    """快捷指令重试时靠它去重，允许为 NULL 但不允许重复。"""
    con = _fresh(work_dir)
    _insert_item(con, title="A", priority=1)
    con.execute("UPDATE items SET client_uuid = 'abc' WHERE id = 1")
    _insert_item(con, title="B", priority=2)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE items SET client_uuid = 'abc' WHERE id = 2")


def test_item_indexes_exist(work_dir: Path) -> None:
    """两张表的排序查询各依赖一个索引；缺了会随数据增长退化成全表扫描。"""
    con = _fresh(work_dir)
    names = {row["name"] for row in con.execute("PRAGMA index_list('items')")}
    assert "idx_items_status_due" in names
    assert "idx_items_status_prio" in names


# --------------------------------------------------------------------------- #
# 外键
# --------------------------------------------------------------------------- #
def test_foreign_keys_are_enabled(work_dir: Path) -> None:
    con = _fresh(work_dir)
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_course_delete_cascades_to_sessions(work_dir: Path) -> None:
    con = _fresh(work_dir)
    con.execute(
        "INSERT INTO courses (name, created_at, updated_at) VALUES ('电子电路基础', 'x', 'x')"
    )
    course_id = con.execute("SELECT id FROM courses").fetchone()["id"]
    con.execute(
        "INSERT INTO course_sessions (course_id, weekday, start_time, end_time, weeks)"
        " VALUES (?, 3, '08:00', '09:40', '1-16')",
        (course_id,),
    )
    con.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    assert con.execute("SELECT COUNT(*) AS n FROM course_sessions").fetchone()["n"] == 0


def test_course_delete_nulls_memory_reference(work_dir: Path) -> None:
    """记忆不该因为课程被删而消失——删课程是排课调整，不是遗忘。"""
    con = _fresh(work_dir)
    con.execute(
        "INSERT INTO courses (name, created_at, updated_at) VALUES ('复变函数', 'x', 'x')"
    )
    course_id = con.execute("SELECT id FROM courses").fetchone()["id"]
    con.execute(
        "INSERT INTO memories (title, content, scope, course_id, created_at, updated_at)"
        " VALUES ('复变作业', '周三交', 'course', ?, 'x', 'x')",
        (course_id,),
    )
    con.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    row = con.execute("SELECT course_id, content FROM memories").fetchone()
    assert row["course_id"] is None
    assert row["content"] == "周三交"


def test_session_rejects_reversed_time_range(work_dir: Path) -> None:
    con = _fresh(work_dir)
    con.execute(
        "INSERT INTO courses (name, created_at, updated_at) VALUES ('概率论', 'x', 'x')"
    )
    course_id = con.execute("SELECT id FROM courses").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time, end_time, weeks)"
            " VALUES (?, 1, '10:00', '08:00', '1-16')",
            (course_id,),
        )


def test_memory_scope_enum(work_dir: Path) -> None:
    con = _fresh(work_dir)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO memories (title, content, scope, created_at, updated_at)"
            " VALUES ('x', 'y', 'session', 'x', 'x')"
        )


def test_api_key_hash_is_unique(work_dir: Path) -> None:
    con = _fresh(work_dir)
    con.execute("INSERT INTO api_keys (name, key_hash, created_at) VALUES ('a', 'h', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO api_keys (name, key_hash, created_at) VALUES ('b', 'h', 'x')")


# --------------------------------------------------------------------------- #
# 事务
# --------------------------------------------------------------------------- #
def test_transaction_rolls_back_on_error(work_dir: Path) -> None:
    con = _fresh(work_dir)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(con):
            _insert_item(con, title="会被回滚", priority=1)
            _insert_item(con, title="非法", due_at=None, priority=None)
    assert con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


def test_transaction_commits_on_success(work_dir: Path) -> None:
    con = _fresh(work_dir)
    with transaction(con):
        _insert_item(con, title="保留", priority=1)
    assert con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1


def test_wal_mode_enabled(work_dir: Path) -> None:
    con = _fresh(work_dir)
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
