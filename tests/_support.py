"""测试用的小工具。

放在独立模块而不是 conftest 里：conftest 只放 fixture，工具函数放这里，
避免 conftest 越写越长，也方便在非 pytest 上下文（比如脚本）里复用。
"""

from __future__ import annotations

import os
from pathlib import Path

import tomlkit

from app.config import Config, load_config

#: 测试配置里固定的时区。用固定值而不是读本机时区：
#: 同一份测试在不同机器上必须得到同样的结果。
TEST_TZ = "Asia/Shanghai"

VALID_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
#: 真实可用的 scrypt 哈希，明文是 "test-password"。
#: 刻意用真哈希而不是占位串：登录鉴权测试必须跑真实的校验路径，
#: 否则哈希格式写错了测试也发现不了。
VALID_PASSWORD = "test-password"
VALID_PASSWORD_HASH = (
    "scrypt$n=16384,r=8,p=1$yGKM5H2DiRTzFMDib+bWAQ==$"
    "hpwNpyeBFIdJh5nePsBpc3Uzj5g2swghtc+Gzhi/fds="
)

CONFIG_TEMPLATE = """\
# 测试配置。注释存在的意义是验证写回时不会把注释吃掉。
[server]
# 测试里刻意用 http://127.0.0.1 —— 生产是 https 的 :8443，
# 这里用 http 是为了让 "secure Cookie 只在 HTTPS 下加" 这条逻辑可被两侧覆盖。
public_url  = "http://127.0.0.1:8000"
timezone    = "{tz}"
listen_host = "127.0.0.1"
listen_port = 8000
data_dir    = "data"

[auth]
password_hash = "{password_hash}"
secret_key    = "{secret_key}"
session_ttl_days = 30
session_epoch    = 1

[term]
start_date  = "2026-09-14"
total_weeks = 16

[llm]
provider = "mock"
base_url = "https://api.deepseek.com"
model    = "deepseek-flash"
api_key  = "test-key"
thinking = false
temperature = 1.0
timeout_seconds = 60
max_tokens = 32000
system_prompt = "测试用提示词"

[ingest]
confirm_ttl_hours = 48
max_items = 20
memory_char_budget = 2000
"""


def write_config(
    directory: Path,
    *,
    overrides: dict[str, dict[str, object]] | None = None,
    password_hash: str = VALID_PASSWORD_HASH,
    secret_key: str = VALID_SECRET,
    name: str = "config.toml",
) -> Path:
    """写一份可用的测试配置，返回路径。

    ``overrides`` 是 ``{"段名": {"键": 值}}``，会在模板基础上就地覆盖。
    用 tomlkit 而不是字符串替换：值里带引号或换行时字符串替换会写坏文件。
    """
    doc = tomlkit.parse(
        CONFIG_TEMPLATE.format(tz=TEST_TZ, password_hash=password_hash, secret_key=secret_key)
    )
    for section, values in (overrides or {}).items():
        if section not in doc:
            doc[section] = tomlkit.table()
        for key, value in values.items():
            doc[section][key] = value

    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return path


def make_config(directory: Path, **kwargs: object) -> Config:
    """写好配置并加载，返回 :class:`Config`。"""
    path = write_config(directory, **kwargs)  # type: ignore[arg-type]
    return load_config(path)


def clear_env_overrides() -> None:
    """清掉可能干扰测试的环境变量覆盖。

    配置文件优先于内置默认值，但**环境变量优先于配置文件**——
    开发机上如果 export 过 SK_LLM_API_KEY，测试读到的就不是测试里写的值。
    """
    for name in ("SK_CONFIG", "SK_SECRET_KEY", "SK_PASSWORD_HASH", "SK_LLM_API_KEY",
                 "SK_DUCKDNS_TOKEN"):
        os.environ.pop(name, None)
