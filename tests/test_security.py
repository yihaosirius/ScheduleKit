"""鉴权原语测试。

这些是**安全性质**的测试，不是覆盖率测试。每一条都对应一种真实攻击或真实事故：

* 密码哈希：salt 必须随机（相同密码两次哈希不同）、参数写进串里、校验必须常量时间口径。
* 会话：篡改签名/改 epoch/过期/未来时间都必须被拒。
* CSRF：伪造的 token（哪怕是"Cookie 与表单值相等"的双提交）也必须被拒。
* API Key：库里只有哈希、明文只出现一次。
"""

from __future__ import annotations

import hashlib
import time

import pytest

from app.security import (
    SCRYPT_N,
    generate_api_key,
    hash_api_key,
    hash_password,
    issue_csrf,
    issue_session,
    looks_like_api_key,
    parse_session,
    should_refresh,
    verify_csrf,
    verify_password,
)

SECRET = "test-secret-key"


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def test_hash_is_verifiable() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)


def test_wrong_password_rejected() -> None:
    stored = hash_password("right")
    assert not verify_password("wrong", stored)
    assert not verify_password("", stored)


def test_same_password_hashes_differently() -> None:
    """salt 必须随机。相同哈希意味着可彩虹表/可批量比对。"""
    a = hash_password("same")
    b = hash_password("same")
    assert a != b
    assert verify_password("same", a) and verify_password("same", b)


def test_hash_format_carries_parameters() -> None:
    stored = hash_password("x")
    prefix, params, salt, digest = stored.split("$")
    assert prefix == "scrypt"
    assert params == f"n={SCRYPT_N},r=8,p=1"
    assert salt and digest


def test_custom_parameters_roundtrip() -> None:
    """参数写进串里，才能在不迁移旧哈希的前提下调参。"""
    stored = hash_password("x", n=1024, r=8, p=1)
    assert "n=1024" in stored
    assert verify_password("x", stored)


@pytest.mark.parametrize(
    "broken",
    [
        "",
        "not-a-hash",
        "scrypt$n=16384,r=8,p=1$onlythree",
        "bcrypt$n=16384,r=8,p=1$c2FsdA==$ZGlk",
        "scrypt$n=x,r=8,p=1$c2FsdA==$ZGlk",
        "scrypt$n=16384,r=8,p=1$!!!not base64!!!$ZGlk",
    ],
)
def test_malformed_hash_is_rejected_without_raising(broken: str) -> None:
    """格式非法时返回 False 而不是抛错：抛错等于告诉攻击者"格式对不对"。"""
    assert verify_password("x", broken) is False


def test_empty_password_cannot_be_hashed() -> None:
    with pytest.raises(ValueError):
        hash_password("")


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def test_session_roundtrip() -> None:
    token = issue_session(SECRET, epoch=3)
    session = parse_session(token, SECRET, ttl_days=30, epoch=3)
    assert session is not None
    assert session.epoch == 3


def test_tampered_signature_rejected() -> None:
    token = issue_session(SECRET, epoch=1)
    issued, epoch, signature = token.split(".")
    forged = f"{issued}.{epoch}.{'0' * len(signature)}"
    assert parse_session(forged, SECRET, ttl_days=30, epoch=1) is None


def test_tampered_epoch_rejected() -> None:
    """把 epoch 改大就能绕过"改密码踢会话"——必须签名覆盖 epoch。"""
    token = issue_session(SECRET, epoch=1)
    issued, _, signature = token.split(".")
    assert parse_session(f"{issued}.9.{signature}", SECRET, ttl_days=30, epoch=9) is None


def test_epoch_mismatch_rejected() -> None:
    """密码改过（epoch 自增）后，旧会话立即失效。"""
    token = issue_session(SECRET, epoch=1)
    assert parse_session(token, SECRET, ttl_days=30, epoch=2) is None


def test_other_secret_rejected() -> None:
    token = issue_session(SECRET, epoch=1)
    assert parse_session(token, "another-secret", ttl_days=30, epoch=1) is None


def test_expired_session_rejected() -> None:
    now = int(time.time())
    token = issue_session(SECRET, epoch=1, now=now - 31 * 86400)
    assert parse_session(token, SECRET, ttl_days=30, epoch=1, now=now) is None


def test_session_just_inside_ttl_accepted() -> None:
    now = int(time.time())
    token = issue_session(SECRET, epoch=1, now=now - 29 * 86400)
    assert parse_session(token, SECRET, ttl_days=30, epoch=1, now=now) is not None


def test_far_future_session_rejected() -> None:
    """签名正确但时间在很久以后，说明密钥泄漏或时钟被动手脚。"""
    now = int(time.time())
    token = issue_session(SECRET, epoch=1, now=now + 86400)
    assert parse_session(token, SECRET, ttl_days=30, epoch=1, now=now) is None


def test_small_clock_skew_allowed() -> None:
    now = int(time.time())
    token = issue_session(SECRET, epoch=1, now=now + 60)
    assert parse_session(token, SECRET, ttl_days=30, epoch=1, now=now) is not None


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "....", "abc"])
def test_malformed_session_token_rejected(token: str) -> None:
    assert parse_session(token, SECRET, ttl_days=30, epoch=1) is None


def test_empty_secret_rejects_everything() -> None:
    token = issue_session(SECRET, epoch=1)
    assert parse_session(token, "", ttl_days=30, epoch=1) is None


def test_sliding_refresh_threshold() -> None:
    now = int(time.time())
    old = parse_session(
        issue_session(SECRET, 1, now=now - 2 * 86400), SECRET, ttl_days=30, epoch=1, now=now
    )
    fresh = parse_session(
        issue_session(SECRET, 1, now=now - 60), SECRET, ttl_days=30, epoch=1, now=now
    )
    assert old is not None and fresh is not None
    assert should_refresh(old) is True
    assert should_refresh(fresh) is False


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
def test_csrf_roundtrip() -> None:
    assert verify_csrf(issue_csrf(SECRET), SECRET)


def test_csrf_tokens_are_unique() -> None:
    assert issue_csrf(SECRET) != issue_csrf(SECRET)


def test_csrf_rejects_double_submit_forgery() -> None:
    """单纯"Cookie 与表单值相等"是挡不住能写 Cookie 的攻击者的。

    攻击者能构造任意 ``nonce``，但他造不出 ``HMAC(secret, "csrf:"+nonce)``，
    所以必须被拒。
    """
    forged = f"{'a' * 16}.{'0' * 64}"
    assert verify_csrf(forged, SECRET) is False


def test_csrf_rejects_other_secret() -> None:
    assert verify_csrf(issue_csrf(SECRET), "other-secret") is False


@pytest.mark.parametrize("token", ["", "no-dot", "a.b.c", "."])
def test_malformed_csrf_rejected(token: str) -> None:
    assert verify_csrf(token, SECRET) is False


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
def test_api_key_shape() -> None:
    plain, digest, prefix = generate_api_key()
    assert plain.startswith("sk_")
    assert len(plain) > 24
    assert digest == hashlib.sha256(plain.encode()).hexdigest()
    assert plain.startswith(prefix)
    assert len(prefix) < len(plain), "前缀不能等于明文，否则列表页会泄漏 key"


def test_api_keys_are_unique() -> None:
    keys = {generate_api_key()[0] for _ in range(50)}
    assert len(keys) == 50


def test_api_key_hash_is_deterministic() -> None:
    plain, digest, _ = generate_api_key()
    assert hash_api_key(plain) == digest


def test_looks_like_api_key() -> None:
    plain, _, _ = generate_api_key()
    assert looks_like_api_key(plain)
    assert not looks_like_api_key("Bearer x")
    assert not looks_like_api_key("sk_short")
