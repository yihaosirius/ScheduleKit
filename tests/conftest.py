"""测试公共装置。

两条沙箱相关的取舍（见 docs/dev-trace.md 记录）：

1. ``tmp_path`` 不能用。沙箱里在 ``%TEMP%`` 下建目录再清理会 ``PermissionError``，
   而 pytest 的默认 ``tmp_path`` 走的正是那条路。这里改用工作区内的
   ``.pytest-run/``，并且**不做清理**——留着反而方便事后翻现场。
2. ``-p no:cacheprovider`` 必须加。已经写进 AGENTS.md 与 README，不靠记忆。

``tests._support`` 提供建配置、建库、建 app 的小工具，避免每个测试文件各写一份。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_ROOT = PROJECT_ROOT / ".pytest-run"

#: 沙箱里 uv 的缓存目录也可能不可写，统一重定向。
os.environ.setdefault("UV_CACHE_DIR", str(Path(os.environ.get("TEMP", "/tmp")) / "sk-uvcache"))

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _prepare_run_root() -> None:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试都在干净的环境变量下跑。

    环境变量**优先于**配置文件（见 app.config.ENV_OVERRIDES），
    所以开发机上 export 过的 SK_LLM_API_KEY 会悄悄污染测试结果。
    """
    from tests._support import clear_env_overrides

    clear_env_overrides()
    monkeypatch.delenv("SK_CONFIG", raising=False)


@pytest.fixture
def work_dir(request: pytest.FixtureRequest) -> Path:
    """每个测试一个独立目录。

    先尝试清空复用目录；沙箱里目录删除可能被拒（WinError 5），
    那时退化成"每次换一个新名字"而不是让测试失败——测试装置不该因为
    宿主环境限制而阻断被测逻辑。
    """
    safe = request.node.name.replace("/", "_").replace("\\", "_").replace(":", "_")
    path = RUN_ROOT / safe
    if path.exists():
        try:
            shutil.rmtree(path)
        except OSError:
            path = RUN_ROOT / f"{safe}-{len(list(RUN_ROOT.iterdir()))}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# 端到端装置
# --------------------------------------------------------------------------- #
@pytest.fixture
def configured(work_dir: Path):
    """已在临时目录建好配置 + 数据库的 :class:`Config`。"""
    from app.config import load_config
    from app.db import connect
    from app.migrations import runner
    from app.paths import ensure_dirs
    from tests._support import write_config

    path = write_config(work_dir)
    cfg = load_config(path)
    ensure_dirs(cfg)
    con = connect(cfg.db_path)
    runner.run(con)
    con.close()
    return cfg


@pytest.fixture
async def api(configured):
    """已登录的 HTTP 客户端。

    用 ``httpx.ASGITransport`` 直连应用，不起真端口——测试因此不需要
    处理端口占用与启动等待。CSRF 与 Cookie 由同一个 client 维护，
    与浏览器的行为一致。
    """
    import httpx

    from app.main import create_app
    from tests._support import VALID_PASSWORD

    app = create_app(configured)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=str(configured.server.public_url),
        follow_redirects=False,
    ) as client:
        # 先拿 CSRF（/me 会给一个），再登录
        await client.get("/api/auth/me")
        response = await client.post("/api/auth/login", json={"password": VALID_PASSWORD})
        assert response.status_code == 200, response.text
        # 登录会换一份新的 cookie，重新取 CSRF
        await client.get("/api/auth/me")
        client.headers["X-CSRF-Token"] = client.cookies.get("sk_csrf", "")
        yield client


@pytest.fixture
async def anon(configured):
    """未登录的客户端，用于验证鉴权边界。"""
    import httpx

    from app.main import create_app

    app = create_app(configured)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=str(configured.server.public_url), follow_redirects=False
    ) as client:
        yield client


@pytest.fixture
def bare_con(configured):
    """直接操作数据库的连接，用于构造前置数据。"""
    from app.db import connect

    con = connect(configured.db_path)
    yield con
    con.close()

