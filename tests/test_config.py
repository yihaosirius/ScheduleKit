"""配置读写测试。

这里守的是几条"出错会很难查"的性质，而不是覆盖率：

* 写回保留注释 —— 控制台每保存一次吃一批注释，配置文件几轮就废了。
* 原子替换 —— 半截文件会让服务下次起不来，且现场已经没了。
* 环境变量覆盖 —— 部署方式依赖它，且优先级写反了会让"我明明配了却不生效"。
* 派生路径以配置文件所在目录为基准 —— 相对 data_dir 的语义必须稳定。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import tomlkit

from app.config import (
    Config,
    ConfigError,
    load_config,
    read_raw,
    update_config_section,
)
from tests._support import make_config, write_config

EXPECTED_SECTIONS = ("server", "auth", "term", "llm", "ingest", "tls", "backup")


def test_load_reads_every_section(work_dir: Path) -> None:
    cfg = make_config(work_dir)

    assert cfg.server.timezone == "Asia/Shanghai"
    assert cfg.server.listen_port == 8000
    assert cfg.auth.session_ttl_days == 30
    assert cfg.term.start_date == "2026-09-14"
    assert cfg.term.total_weeks == 16
    assert cfg.llm.provider == "mock"
    assert cfg.llm.model == "deepseek-flash"
    assert cfg.ingest.confirm_ttl_hours == 48
    # 模板里没写 [tls] / [backup]，应落到内置默认值而不是报错
    assert cfg.tls.domain == "canisa1ph.duckdns.org"
    assert cfg.backup.enabled is True


def test_base_url_trailing_slash_is_trimmed(work_dir: Path) -> None:
    """拼端点时用的是 f"{base_url}/responses"，多一个斜杠会拼出 //responses。"""
    cfg = make_config(
        work_dir, overrides={"llm": {"base_url": "https://api.deepseek.com/"}}
    )
    assert cfg.llm.base_url == "https://api.deepseek.com"


def test_default_system_prompt_applied_when_empty(work_dir: Path) -> None:
    cfg = make_config(work_dir, overrides={"llm": {"system_prompt": ""}})
    assert "submit_tasks" in cfg.llm.system_prompt
    assert len(cfg.llm.system_prompt) > 100


def test_relative_data_dir_resolves_against_config_file(work_dir: Path) -> None:
    """相对 data_dir 必须跟着配置文件走，而不是跟着 CWD 走。

    生产上 systemd 的 WorkingDirectory 变了不该导致数据写到别处。
    """
    nested = work_dir / "etc" / "schedulekit"
    cfg = make_config(nested)

    assert cfg.data_dir == (nested / "data").resolve()
    assert cfg.db_path == (nested / "data" / "schedulekit.db").resolve()
    assert cfg.uploads_dir == (nested / "data" / "uploads").resolve()


def test_absolute_data_dir_used_verbatim(work_dir: Path) -> None:
    target = work_dir / "var-lib"
    cfg = make_config(work_dir, overrides={"server": {"data_dir": str(target)}})
    assert cfg.data_dir == target


def test_missing_explicit_path_is_an_error_not_a_default(work_dir: Path) -> None:
    """显式指定了不存在的路径，是配置错误，不能静默用默认值。"""
    with pytest.raises(ConfigError):
        load_config(work_dir / "nope.toml")


def test_parse_error_is_wrapped_with_path(work_dir: Path) -> None:
    broken = work_dir / "broken.toml"
    broken.write_text("[server\npublic_url = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(broken)
    assert "broken.toml" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 环境变量覆盖
# --------------------------------------------------------------------------- #
def test_env_overrides_file(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SK_LLM_API_KEY", "from-env")
    cfg = make_config(work_dir)
    assert cfg.llm.api_key == "from-env"


def test_env_override_for_secret_key(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SK_SECRET_KEY", "env-secret")
    cfg = make_config(work_dir)
    assert cfg.auth.secret_key == "env-secret"


def test_empty_env_var_does_not_override(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空字符串不算覆盖。否则 CI 里 export SK_LLM_API_KEY= 会把配置清空。"""
    monkeypatch.setenv("SK_LLM_API_KEY", "")
    cfg = make_config(work_dir)
    assert cfg.llm.api_key == "test-key"


def test_sk_config_env_selects_file(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(work_dir, overrides={"server": {"listen_port": 9999}})
    monkeypatch.setenv("SK_CONFIG", str(path))
    cfg = load_config()
    assert cfg.server.listen_port == 9999
    assert cfg.path == path.resolve()


# --------------------------------------------------------------------------- #
# 写回
# --------------------------------------------------------------------------- #
def test_update_preserves_comments_and_other_sections(work_dir: Path) -> None:
    path = write_config(work_dir)
    original = path.read_text(encoding="utf-8")

    update_config_section(path, "llm", {"model": "deepseek-v4-pro", "temperature": 0.3})

    after = path.read_text(encoding="utf-8")
    # 注释还在
    assert "# 测试配置。注释存在的意义是验证写回时不会把注释吃掉。" in after
    # 其它段没有被重排或丢失
    assert "[term]" in after
    assert 'start_date  = "2026-09-14"' in after
    # 目标键真的变了
    cfg = load_config(path)
    assert cfg.llm.model == "deepseek-v4-pro"
    assert cfg.llm.temperature == 0.3
    # 并且没有留下临时文件
    assert [p.name for p in work_dir.glob("*.tmp")] == []
    assert original != after


def test_update_creates_missing_section(work_dir: Path) -> None:
    path = write_config(work_dir)
    update_config_section(path, "tls", {"domain": "example.duckdns.org", "port": 8443})

    cfg = load_config(path)
    assert cfg.tls.domain == "example.duckdns.org"
    assert cfg.tls.port == 8443


def test_update_json_roundtrip(work_dir: Path) -> None:
    """read_raw 供部署脚本用（install.sh 不自己解析 TOML）。"""
    path = write_config(work_dir)
    raw = read_raw(path)
    assert raw["server"]["listen_port"] == 8000
    assert raw["term"]["total_weeks"] == 16


def test_update_section_rejects_nothing_but_writes_atomically(work_dir: Path) -> None:
    """写失败时不能留下半截文件。"""
    path = write_config(work_dir)
    before = path.read_text(encoding="utf-8")

    class Unserializable:
        pass

    with pytest.raises(Exception):
        update_config_section(path, "llm", {"model": Unserializable()})

    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in work_dir.glob("*.tmp")] == []


def test_written_file_keeps_owner_mode(work_dir: Path) -> None:
    """os.replace 会替换 inode，权限必须显式搬过去。

    0600 是 config.toml 的约定权限（含 LLM API Key）；如果原子替换后
    变成默认的 0644，密钥就对同机其它用户可读了。
    """
    path = write_config(work_dir)
    if os.name == "nt":  # Windows 上 chmod 基本无效，跳过断言
        pytest.skip("Windows 不支持 POSIX 权限位")
    os.chmod(path, 0o600)
    update_config_section(path, "llm", {"model": "x"})
    assert (path.stat().st_mode & 0o777) == 0o600


# --------------------------------------------------------------------------- #
# 启动期校验
# --------------------------------------------------------------------------- #
def _valid_config(work_dir: Path, **overrides: object) -> Config:
    return make_config(work_dir, overrides=overrides)  # type: ignore[arg-type]


def test_validate_passes_with_required_fields(work_dir: Path) -> None:
    _valid_config(work_dir).validate_for_startup()


def test_validate_rejects_empty_secret_key(work_dir: Path) -> None:
    cfg = make_config(work_dir, secret_key="")
    with pytest.raises(ConfigError) as excinfo:
        cfg.validate_for_startup()
    assert "SK_SECRET_KEY" in str(excinfo.value)


def test_validate_rejects_empty_password_hash(work_dir: Path) -> None:
    cfg = make_config(work_dir, password_hash="")
    with pytest.raises(ConfigError) as excinfo:
        cfg.validate_for_startup()
    assert "password" in str(excinfo.value).lower()


def test_validate_rejects_non_loopback_listen(work_dir: Path) -> None:
    """对外必须经 Caddy。直接暴露应用会绕过 TLS 与安全响应头。"""
    cfg = make_config(work_dir, overrides={"server": {"listen_host": "0.0.0.0"}})
    with pytest.raises(ConfigError) as excinfo:
        cfg.validate_for_startup()
    assert "回环" in str(excinfo.value)


def test_validate_rejects_unknown_llm_provider(work_dir: Path) -> None:
    cfg = make_config(work_dir, overrides={"llm": {"provider": "gemini"}})
    with pytest.raises(ConfigError) as excinfo:
        cfg.validate_for_startup()
    assert "responses" in str(excinfo.value)


def test_llm_validation_skipped_for_mock(work_dir: Path) -> None:
    make_config(work_dir, overrides={"llm": {"provider": "mock", "api_key": ""}}).validate_for_llm()


def test_llm_validation_requires_api_key(work_dir: Path) -> None:
    cfg = make_config(
        work_dir, overrides={"llm": {"provider": "responses", "api_key": ""}}
    )
    with pytest.raises(ConfigError) as excinfo:
        cfg.validate_for_llm()
    assert "SK_LLM_API_KEY" in str(excinfo.value)


def test_llm_validation_rejects_relative_base_url(work_dir: Path) -> None:
    cfg = make_config(
        work_dir,
        overrides={"llm": {"provider": "responses", "base_url": "api.deepseek.com"}},
    )
    with pytest.raises(ConfigError):
        cfg.validate_for_llm()


# --------------------------------------------------------------------------- #
# 模板漂移
# --------------------------------------------------------------------------- #
def test_example_config_parses_and_matches_defaults() -> None:
    """仓库里的 config.toml.example 必须能被解析，否则部署第一步就炸。"""
    example = Path(__file__).resolve().parent.parent / "config.toml.example"
    assert example.exists(), "仓库缺少 config.toml.example"
    doc = tomlkit.parse(example.read_text(encoding="utf-8"))
    for section in EXPECTED_SECTIONS:
        assert section in doc, f"config.toml.example 缺少 [{section}]"
    # 模板里不能有真实密钥
    assert doc["auth"]["secret_key"] == ""
    assert doc["llm"]["api_key"] == ""
    assert doc["tls"]["duckdns_token"] == ""
