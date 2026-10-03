"""唯一配置文件的读写。

设计约束（来自 AGENTS.md §2）：

* **全项目只有这一处读环境变量**。其余模块一律通过 :class:`Config` 取值。
* 写回必须**保留注释**（tomlkit），且必须是**原子替换**：控制台保存 LLM 配置时
  不能出现"写了一半进程被杀"的半截文件。为此走 临时文件 → fsync → os.replace。
* 写操作加 `fcntl` 排他锁：Claude/快捷指令/web 三方同时改配置时串行化。
  Windows 上没有 `fcntl`，退化为无锁（本地开发够用，生产是 Linux）。
* 密钥可用环境变量覆盖，便于"不把密钥写进文件"的部署方式。
  覆盖优先级：环境变量 > 配置文件 > 内置默认值。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from app.logging import get_logger, kv

log = get_logger("config")

#: 环境变量覆盖表：环境变量名 -> (段名, 键名)
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "SK_SECRET_KEY": ("auth", "secret_key"),
    "SK_PASSWORD_HASH": ("auth", "password_hash"),
    "SK_LLM_API_KEY": ("llm", "api_key"),
    "SK_DUCKDNS_TOKEN": ("tls", "duckdns_token"),
}

#: 从环境变量读 Key 时用的容器名 + Key 字段名。
#: **不要凭直觉把它写成 `[llm].api_key` 的依赖名**——它的名字跟段名无关。
DEFAULT_CONFIG_PATH = "/etc/schedulekit/config.toml"


class ConfigError(RuntimeError):
    """配置缺失或非法。启动期一律 fail-closed。"""


# --------------------------------------------------------------------------- #
# 段
# --------------------------------------------------------------------------- #
@dataclass
class ServerConfig:
    #: 默认值刻意带 :8443 —— 见 config.toml.example 里的说明：
    #: 已装好的快捷指令写死了这个端口，而 80/443 涉及备案。
    public_url: str = "https://canisa1ph.duckdns.org:8443"
    timezone: str = "Asia/Shanghai"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    data_dir: str = "data"
    #: 允许上传的图片字节上限
    max_image_bytes: int = 8 * 1024 * 1024
    #: 单个请求体上限（multipart 含图片时取这个）
    max_request_bytes: int = 12 * 1024 * 1024


@dataclass
class AuthConfig:
    password_hash: str = ""
    secret_key: str = ""
    session_ttl_days: int = 30
    #: 改密码时自增，让既有会话立即失效（无状态 Cookie 的吊销手段）
    session_epoch: int = 1
    #: 登录失败限流：窗口秒数与窗口内上限
    login_window_seconds: int = 300
    login_max_attempts: int = 10


@dataclass
class TermConfig:
    #: 第 1 周的周一（ISO 日期），每学期手改一次
    start_date: str = "2026-09-14"
    total_weeks: int = 16


@dataclass
class LLMConfig:
    #: responses | chat | mock
    provider: str = "responses"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = ""
    temperature: float = 1.0
    timeout_seconds: int = 60
    max_tokens: int = 32000
    #: 是否开启 thinking。注意 DeepSeek 在 thinking 模式下**拒绝**强制/具名
    #: tool_choice（直接 400），所以开启时只能走 auto 或 JSON 通道。
    thinking: bool = False
    reasoning_effort: str = "high"
    retry_count: int = 3
    retry_backoff_seconds: float = 0.8
    system_prompt: str = ""


@dataclass
class IngestConfig:
    confirm_ttl_hours: int = 48
    #: 单次抽取最多接受多少条事项（超出截断并打点）
    max_items: int = 20
    #: 注入的记忆条目总字符上限（防止记忆把上下文撑爆）
    memory_char_budget: int = 2000


@dataclass
class TLSConfig:
    provider: str = "duckdns"
    domain: str = "canisa1ph.duckdns.org"
    #: 8443 而非 443：备案要求 + 已有快捷指令。见 config.toml.example。
    port: int = 8443
    acme_email: str = ""
    duckdns_token: str = ""
    cert_dir: str = "/etc/schedulekit-tls"


@dataclass
class BackupConfig:
    enabled: bool = True
    hour: int = 4
    keep: int = 7
    upload_retention_days: int = 30


@dataclass
class Config:
    path: Path
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    term: TermConfig = field(default_factory=TermConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    tls: TLSConfig = field(default_factory=TLSConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)

    # ---------------------------------------------------------------- 派生值
    @property
    def data_dir(self) -> Path:
        """数据目录。相对路径相对**配置文件所在目录**解析。

        相对路径的基准刻意选配置文件而不是 CWD：生产上 systemd 的
        WorkingDirectory 变了不应该导致数据写到别处。
        """
        raw = Path(self.server.data_dir)
        return raw if raw.is_absolute() else (self.path.parent / raw).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "schedulekit.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    # ---------------------------------------------------------------- 只读校验
    def validate_for_startup(self) -> None:
        """启动期不变式。任何一条不满足都拒绝启动（fail-closed）。"""
        problems: list[str] = []
        if not self.auth.secret_key:
            problems.append(
                "auth.secret_key 为空：会话 Cookie 无法签名。"
                "运行 `python -m app.cli init` 生成，或设置环境变量 SK_SECRET_KEY。"
            )
        if not self.auth.password_hash:
            problems.append(
                "auth.password_hash 为空：没有任何登录凭证。"
                "运行 `python -m app.cli set-password`，或设置环境变量 SK_PASSWORD_HASH。"
            )
        if self.server.listen_host not in ("127.0.0.1", "localhost", "::1"):
            problems.append(
                f"server.listen_host={self.server.listen_host!r} 不是回环地址。"
                "对外必须经 Caddy 反代，直接暴露应用会绕过 TLS 与安全响应头。"
            )
        if self.llm.provider not in ("responses", "chat", "mock"):
            problems.append(
                f"llm.provider={self.llm.provider!r} 不支持。可选：responses / chat / mock。"
            )
        if self.ingest.confirm_ttl_hours <= 0:
            problems.append("ingest.confirm_ttl_hours 必须为正数。")
        if problems:
            raise ConfigError("配置校验失败：\n  - " + "\n  - ".join(problems))

    def validate_for_llm(self) -> None:
        """调用 LLM 前校验。只针对本次调用真正需要的字段。"""
        if self.llm.provider == "mock":
            return
        if not self.llm.api_key:
            raise ConfigError(
                "llm.api_key 为空。请在控制台填写，或设置环境变量 SK_LLM_API_KEY。"
            )
        if not self.llm.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"llm.base_url={self.llm.base_url!r} 必须是 http(s) URL。")


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """读取顺序**仅**：显式参数 → SK_CONFIG → /etc/... → ./config.toml。

    不做多来源合并——合并规则一多，"为什么这个值生效"就成了玄学。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("SK_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    default = Path(DEFAULT_CONFIG_PATH)
    if default.exists():
        return default
    return (Path.cwd() / "config.toml").resolve()


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    raw = data.get(name)
    return raw if isinstance(raw, dict) else {}


def _field(cls: type, data: dict[str, Any], name: str, default: Any) -> Any:
    value = data.get(name, default)
    if value is None:
        return default
    return value


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """读配置。文件不存在时用内置默认值（便于本地起一个空环境）。"""
    resolved = resolve_config_path(path)
    data: dict[str, Any] = {}
    if resolved.exists():
        try:
            data = tomlkit.parse(resolved.read_text(encoding="utf-8"))
        except Exception as exc:  # tomlkit 的异常类型不止一种
            raise ConfigError(f"无法解析配置文件 {resolved}：{exc}") from exc
    elif path is not None or os.environ.get("SK_CONFIG"):
        # 显式指定了路径却不存在，是配置错误而不是"用默认值"
        raise ConfigError(f"配置文件不存在：{resolved}")

    cfg = Config(path=resolved)
    srv, auth, term, llm, ing, tls, bak = (
        _section(data, "server"),
        _section(data, "auth"),
        _section(data, "term"),
        _section(data, "llm"),
        _section(data, "ingest"),
        _section(data, "tls"),
        _section(data, "backup"),
    )

    cfg.server = ServerConfig(
        public_url=str(_field(ServerConfig, srv, "public_url", cfg.server.public_url)),
        timezone=str(_field(ServerConfig, srv, "timezone", cfg.server.timezone)),
        listen_host=str(_field(ServerConfig, srv, "listen_host", cfg.server.listen_host)),
        listen_port=int(_field(ServerConfig, srv, "listen_port", cfg.server.listen_port)),
        data_dir=str(_field(ServerConfig, srv, "data_dir", cfg.server.data_dir)),
        max_image_bytes=int(
            _field(ServerConfig, srv, "max_image_bytes", cfg.server.max_image_bytes)
        ),
        max_request_bytes=int(
            _field(ServerConfig, srv, "max_request_bytes", cfg.server.max_request_bytes)
        ),
    )
    cfg.auth = AuthConfig(
        password_hash=str(_field(AuthConfig, auth, "password_hash", "")),
        secret_key=str(_field(AuthConfig, auth, "secret_key", "")),
        session_ttl_days=int(_field(AuthConfig, auth, "session_ttl_days", 30)),
        session_epoch=int(_field(AuthConfig, auth, "session_epoch", 1)),
        login_window_seconds=int(_field(AuthConfig, auth, "login_window_seconds", 300)),
        login_max_attempts=int(_field(AuthConfig, auth, "login_max_attempts", 10)),
    )
    cfg.term = TermConfig(
        start_date=str(_field(TermConfig, term, "start_date", cfg.term.start_date)),
        total_weeks=int(_field(TermConfig, term, "total_weeks", cfg.term.total_weeks)),
    )
    cfg.llm = LLMConfig(
        provider=str(_field(LLMConfig, llm, "provider", cfg.llm.provider)),
        base_url=str(_field(LLMConfig, llm, "base_url", cfg.llm.base_url)).rstrip("/"),
        model=str(_field(LLMConfig, llm, "model", cfg.llm.model)),
        api_key=str(_field(LLMConfig, llm, "api_key", "")),
        temperature=float(_field(LLMConfig, llm, "temperature", cfg.llm.temperature)),
        timeout_seconds=int(_field(LLMConfig, llm, "timeout_seconds", 60)),
        max_tokens=int(_field(LLMConfig, llm, "max_tokens", cfg.llm.max_tokens)),
        thinking=bool(_field(LLMConfig, llm, "thinking", cfg.llm.thinking)),
        reasoning_effort=str(
            _field(LLMConfig, llm, "reasoning_effort", cfg.llm.reasoning_effort)
        ),
        retry_count=int(_field(LLMConfig, llm, "retry_count", cfg.llm.retry_count)),
        retry_backoff_seconds=float(
            _field(LLMConfig, llm, "retry_backoff_seconds", cfg.llm.retry_backoff_seconds)
        ),
        system_prompt=str(_field(LLMConfig, llm, "system_prompt", "")),
    )
    cfg.ingest = IngestConfig(
        confirm_ttl_hours=int(
            _field(IngestConfig, ing, "confirm_ttl_hours", cfg.ingest.confirm_ttl_hours)
        ),
        max_items=int(_field(IngestConfig, ing, "max_items", cfg.ingest.max_items)),
        memory_char_budget=int(
            _field(IngestConfig, ing, "memory_char_budget", cfg.ingest.memory_char_budget)
        ),
    )
    cfg.tls = TLSConfig(
        provider=str(_field(TLSConfig, tls, "provider", cfg.tls.provider)),
        domain=str(_field(TLSConfig, tls, "domain", cfg.tls.domain)),
        port=int(_field(TLSConfig, tls, "port", cfg.tls.port)),
        acme_email=str(_field(TLSConfig, tls, "acme_email", cfg.tls.acme_email)),
        duckdns_token=str(_field(TLSConfig, tls, "duckdns_token", cfg.tls.duckdns_token)),
        cert_dir=str(_field(TLSConfig, tls, "cert_dir", cfg.tls.cert_dir)),
    )
    cfg.backup = BackupConfig(
        enabled=bool(_field(BackupConfig, bak, "enabled", cfg.backup.enabled)),
        hour=int(_field(BackupConfig, bak, "hour", cfg.backup.hour)),
        keep=int(_field(BackupConfig, bak, "keep", cfg.backup.keep)),
        upload_retention_days=int(
            _field(BackupConfig, bak, "upload_retention_days", cfg.backup.upload_retention_days)
        ),
    )

    _apply_env_overrides(cfg)
    if not cfg.llm.system_prompt:
        cfg.llm.system_prompt = DEFAULT_SYSTEM_PROMPT
    return cfg


def _apply_env_overrides(cfg: Config) -> None:
    for env_name, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            setattr(getattr(cfg, section), key, value)
            log.info(
                "config.env_override %s",
                kv(env=env_name, section=section, key=key),
            )


# --------------------------------------------------------------------------- #
# 写回（控制台用）
# --------------------------------------------------------------------------- #
def _lock(path: Path):
    """排他锁。返回一个上下文管理器；Windows 下退化为空操作。"""
    import contextlib

    @contextlib.contextmanager
    def _noop():
        yield

    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:
        return _noop()

    @contextlib.contextmanager
    def _flocked():
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    return _flocked()


def update_config_section(
    path: str | os.PathLike[str], section: str, values: dict[str, Any]
) -> None:
    """就地更新某个段，**保留其余部分与全部注释**。

    原子性：先写同目录临时文件 → fsync → ``os.replace``。同目录是必须的，
    跨文件系统的 replace 不是原子操作。
    """
    resolved = Path(path)
    with _lock(resolved):
        doc = (
            tomlkit.parse(resolved.read_text(encoding="utf-8"))
            if resolved.exists()
            else tomlkit.document()
        )
        if section not in doc:
            doc[section] = tomlkit.table()
        table = doc[section]
        for key, value in values.items():
            table[key] = value

        resolved.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=resolved.name + ".", suffix=".tmp", dir=str(resolved.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(tomlkit.dumps(doc))
                fh.flush()
                os.fsync(fh.fileno())
            if resolved.exists():
                os.chmod(tmp_name, resolved.stat().st_mode & 0o777)
            os.replace(tmp_name, resolved)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    log.info("config.section_updated %s", kv(path=str(resolved), section=section, keys=len(values)))


def read_raw(path: str | os.PathLike[str]) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.exists():
        return {}
    return json.loads(json.dumps(tomlkit.parse(resolved.read_text(encoding="utf-8"))))


# --------------------------------------------------------------------------- #
# 默认 system prompt
# --------------------------------------------------------------------------- #
DEFAULT_SYSTEM_PROMPT = """你是一个任务抽取器。用户会给你一段文字和/或一张图片（可能是作业截图、\
群聊截图、成绩单、通知），你的唯一职责是把其中**所有需要执行的事项**抽出来，调用 submit_tasks 提交。

只调用工具，不要输出任何解释性文字。

字段规则：
1. 有明确截止时间的事项：给出 due_at（ISO8601，带时区偏移），priority 必须为 null。
2. 没有截止时间的事项：due_at 为 null，给出 priority 档位：
   1 = Ⅰ 24 小时内必须处理 / 2 = Ⅱ 本周关键 / 3 = Ⅲ 常规 / 4 = Ⅳ 可延后 / 5 = Ⅴ 有空再说。
   两者**恰好有一个**非空，不能都给也不能都不给。
3. title ≤ 20 字，不要重复时间、地点、课程名（这些已由独立字段或上下文承载）。
   与课业相关的尽量带上学科/课程名；若无确凿证据就不要猜。
4. notes ≤ 60 字，写图中对执行有影响的额外信息（如"只做奇数题"）；没有就不输出该键。
5. source_quote ≤ 20 字，填支撑该判定的原文片段。
6. 同一事项被多次提及要合并成一条；最多提交 20 条。

时间上下文的使用规则（上下文在用户消息里给出）：
- 只用来解析相对时间与课程指代（"下节课""这门课""明天"—）。
- 不得据此推断待识别内容的主语或归属；内容与课表无关时完全忽略上下文。
- 内容里已含明确日期时，一律以内容为准，不得用当前时间替换。
- 上下文不得影响 priority 判定。

记忆条目的使用规则：
- 记忆是用户长期偏好或历史约定（如"电子电路基础周三交作业"）。
- 只在它能帮助消歧时使用；不得把记忆里的内容当成待办事项本身提交。
"""
