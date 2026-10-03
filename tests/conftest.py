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
