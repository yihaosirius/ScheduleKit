"""鉴权原语：密码哈希、会话 Cookie、CSRF、API Key。

**只用标准库**（``hashlib`` / ``hmac`` / ``secrets``），不引第三方鉴权库：

* 密码用 ``hashlib.scrypt``（stdlib，内存硬，抗 GPU）。
* 会话是无状态签名 Cookie —— 单用户应用不需要服务端 session 表，
  改密码时通过 ``session_epoch`` 自增让所有旧会话立即失效。
* CSRF 用「双提交 + 与签名密钥绑定」：值必须由服务端密钥派生，
  这样攻击者即使能写 Cookie 也造不出合法 token。
* API Key 用 32 字节随机串，库里只存 sha256。选 sha256 而不是 scrypt：
  Key 本身是高熵随机串，不存在弱口令问题，而每请求跑一次 scrypt 会让接口慢到不可用。

一个刻意的选择：**所有比较一律用 ``hmac.compare_digest``**。字符串 ``==``
的短路行为会泄漏前缀信息，这类问题在个人项目里也不值得留。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
#: scrypt 参数。n=16384 在本机约 50–80ms，登录场景完全够用。
#: 内存占用 128*n*r ≈ 16MB，注意服务器只有 1.6G —— 不能再往上调一个数量级。
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

PASSWORD_HASH_PREFIX = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """产出 ``scrypt$n=16384,r=8,p=1$<salt_b64>$<digest_b64>``。

    把参数写进哈希串里，是为了将来能调参而不影响旧哈希的校验
    （不同用户的哈希可以带不同参数）。
    """
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN
    )
    return f"{PASSWORD_HASH_PREFIX}$n={n},r={r},p={p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验。任何解析失败都返回 False（不要抛错，避免探测有效格式）。"""
    if not password or not stored:
        return False
    try:
        prefix, params, salt_b64, digest_b64 = stored.split("$")
        if prefix != PASSWORD_HASH_PREFIX:
            return False
        parsed = dict(item.split("=") for item in params.split(","))
        n, r, p = int(parsed["n"]), int(parsed["r"]), int(parsed["p"])
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
    except (ValueError, KeyError, TypeError):
        return False

    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------- #
# 会话 Cookie
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Session:
    issued_at: int
    epoch: int

    @property
    def age_seconds(self) -> int:
        return max(0, int(time.time()) - self.issued_at)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(secret: str, epoch: int, *, now: int | None = None) -> str:
    """产出 ``<issued_at>.<epoch>.<sig>``。"""
    issued = int(now if now is not None else time.time())
    payload = f"{issued}.{epoch}"
    return f"{payload}.{_sign(secret, payload)}"


def parse_session(token: str, secret: str, *, ttl_days: int, epoch: int,
                  now: int | None = None) -> Session | None:
    """校验会话。无效/过期/epoch 不匹配都返回 ``None``。

    ``epoch`` 不匹配即"密码已改"，这是无状态 Cookie 唯一的吊销手段。
    """
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    issued_raw, epoch_raw, signature = parts

    expected = _sign(secret, f"{issued_raw}.{epoch_raw}")
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        issued = int(issued_raw)
        token_epoch = int(epoch_raw)
    except ValueError:
        return None

    if token_epoch != epoch:
        return None

    current = int(now if now is not None else time.time())
    if issued > current + 300:  # 允许 5 分钟时钟漂移，但拒绝未来很久的票
        return None
    if current - issued > ttl_days * 86400:
        return None
    return Session(issued_at=issued, epoch=token_epoch)


def should_refresh(session: Session, *, after_seconds: int = 86400) -> bool:
    """滑动续期：老于一天的会话在下次请求时换新票。

    不每次请求都续期，是为了避免"每个静态资源请求都写一次 Set-Cookie"。
    """
    return session.age_seconds >= after_seconds


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
def issue_csrf(secret: str, *, nonce: str | None = None) -> str:
    """CSRF token = ``<nonce>.<HMAC(secret, "csrf:"+nonce)>``。

    token 必须由服务端密钥派生：单纯的双提交（Cookie 与表单值相等）
    挡不住"能写 Cookie 的攻击者"（子域被拿下、或中间设备注入）。
    """
    value = nonce or secrets.token_hex(8)
    return f"{value}.{_sign(secret, 'csrf:' + value)}"


def verify_csrf(token: str, secret: str) -> bool:
    if not token or not secret:
        return False
    parts = token.split(".")
    if len(parts) != 2:
        return False
    nonce, signature = parts
    return hmac.compare_digest(signature, _sign(secret, "csrf:" + nonce))


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
API_KEY_PREFIX = "sk_"


def generate_api_key() -> tuple[str, str, str]:
    """返回 ``(明文, sha256 十六进制, 展示用前缀)``。

    明文**只在创建时返回一次**；库里只存哈希。返回前缀是为了让控制台
    列表能显示"这个 key 是哪一条"，而无法还原出完整 key。
    """
    raw = secrets.token_urlsafe(32)
    plain = f"{API_KEY_PREFIX}{raw}"
    return plain, hash_api_key(plain), plain[: len(API_KEY_PREFIX) + 6]


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def looks_like_api_key(value: str) -> bool:
    return value.startswith(API_KEY_PREFIX) and len(value) > len(API_KEY_PREFIX) + 16


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
