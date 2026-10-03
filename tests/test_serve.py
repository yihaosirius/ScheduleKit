"""服务入口（``python -m app.serve``）的测试。

这个文件的存在是有教训的：``serve.main`` 里曾经有
``NameError: name 'kv' is not defined``——少了一个 import——而**全部 415 个测试
都是绿的**。原因很简单：测试都通过 ``create_app`` 直接构造应用，
从来没有人调用过真正的启动入口。

这类 bug 的特点是最致命（生产上表现为"服务起不来"）又最容易漏测
（入口代码只有几行，看起来不值得测）。所以这里把入口本身拉进测试范围：
把 ``uvicorn.run`` 换成替身，断言它拿到的是正确的参数。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import serve


@pytest.fixture
def uvicorn_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """拦下 uvicorn.run，记录参数而不真的起服务。"""
    calls: list[dict] = []

    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append({"args": args, "kwargs": kwargs})
        return None

    monkeypatch.setattr(serve.uvicorn, "run", fake_run)
    return calls


def test_main_starts_uvicorn_with_factory(configured, uvicorn_spy: list[dict]) -> None:
    """必须传 factory 而不是应用实例：--reload 时 uvicorn 会在子进程重新导入，
    "每次重载都读最新配置"正是我们要的。"""
    code = serve.main(["--config", str(configured.path), "--port", "8999"])

    assert code == 0
    assert len(uvicorn_spy) == 1
    kwargs = uvicorn_spy[0]["kwargs"]
    assert kwargs["factory"] is True
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8999
    # 访问日志交给我们自己的中间件（带 trace id），避免两套格式
    assert kwargs["access_log"] is False
    # 反代后面要信 X-Real-IP
    assert kwargs["proxy_headers"] is True


def test_defaults_come_from_config(configured, uvicorn_spy: list[dict]) -> None:
    serve.main(["--config", str(configured.path)])
    kwargs = uvicorn_spy[0]["kwargs"]
    assert kwargs["port"] == configured.server.listen_port
    assert kwargs["host"] == configured.server.listen_host


def test_reload_only_watches_app_dir(configured, uvicorn_spy: list[dict]) -> None:
    """重载只盯 app/：data/ 里的截图落盘会触发无意义的重启。"""
    serve.main(["--config", str(configured.path), "--reload"])
    kwargs = uvicorn_spy[0]["kwargs"]
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == ["app"]


def test_no_reload_by_default(configured, uvicorn_spy: list[dict]) -> None:
    serve.main(["--config", str(configured.path)])
    assert uvicorn_spy[0]["kwargs"]["reload"] is False


def test_missing_config_is_a_clean_error(tmp_path: Path, capsys, uvicorn_spy) -> None:
    """配置不存在时给出可读错误并以 2 退出，而不是抛一堆栈。"""
    code = serve.main(["--config", str(tmp_path / "nope.toml")])
    assert code == 2
    assert "nope.toml" in capsys.readouterr().err
    assert uvicorn_spy == [], "配置错误时不该尝试起服务"


def test_log_level_is_normalized(configured, uvicorn_spy: list[dict]) -> None:
    serve.main(["--config", str(configured.path), "--log-level", "debug"])
    assert uvicorn_spy[0]["kwargs"]["log_level"] == "debug"


# --------------------------------------------------------------------------- #
# service 单元与 Caddyfile 的部署约定
# --------------------------------------------------------------------------- #
def test_systemd_unit_points_at_module_entry() -> None:
    """systemd 单元与入口必须一致，否则线上表现是"服务起不来"，
    而本地怎么测都是好的。"""
    unit = Path(__file__).resolve().parent.parent / "deploy" / "schedulekit.service"
    if not unit.exists():
        pytest.skip("deploy/schedulekit.service 尚未创建（部署里程碑）")
    text = unit.read_text(encoding="utf-8")
    assert "-m app.serve" in text
    assert "SK_CONFIG=" in text
    assert "MemoryMax=" in text
    assert "NoNewPrivileges=true" in text
    # 应用只该监听回环：对外的 TLS 与安全响应头都由 Caddy 负责
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=" in text
