"""계정 3단계(문제점 21번, ADR-013) — core/accounts 순수 규칙·core/access 범위 판정."""

import datetime as dt

import pytest

from app.core import access
from app.core import accounts as ac
from app.errors import Forbidden

NOW = dt.datetime(2026, 10, 4, 9, 0, tzinfo=dt.timezone.utc)


def test_password_hash_roundtrip_and_salt():
    h1, h2 = ac.hash_password("secret-pass"), ac.hash_password("secret-pass")
    assert h1 != h2 and h1.startswith("pbkdf2_sha256$")
    assert ac.verify_password("secret-pass", h1) and ac.verify_password("secret-pass", h2)
    assert not ac.verify_password("wrong-pass", h1)
    for bad in (None, "", "bcrypt$x", "pbkdf2_sha256$1$$"):
        assert not ac.verify_password("secret-pass", bad)


def test_expiry_choices():
    assert ac.expires_from("7d", NOW) == NOW + dt.timedelta(days=7)
    assert ac.expires_from("365d", NOW) == NOW + dt.timedelta(days=365)
    assert ac.expires_from("never", NOW) is None
    with pytest.raises(ValueError):
        ac.expires_from("2y", NOW)
    assert set(ac.EXPIRY_DAYS) == {"7d", "15d", "30d", "90d", "180d", "365d", "never"}


def test_is_valid():
    assert ac.is_valid(disabled=False, expires_at=None, now=NOW)
    assert ac.is_valid(disabled=False, expires_at=NOW + dt.timedelta(seconds=1), now=NOW)
    assert not ac.is_valid(disabled=False, expires_at=NOW, now=NOW)
    assert not ac.is_valid(disabled=True, expires_at=None, now=NOW)


def _check(**kw):
    base = dict(username="gyeonggi1", password="password1", role=ac.ROLE_REGION, region_ids=[1],
                reserved={"admin"}, top_ids={1, 2}, super_count=1)
    base.update(kw)
    return ac.check_new_account(**base)


def test_check_new_account_rules():
    assert _check() == {}
    assert "username" in _check(username="ab")
    assert "username" in _check(username="a.b.c")          # '.' 은 쿠키 구분자
    assert "username" in _check(username="admin")          # .env 이름
    assert "password" in _check(password="short")
    assert "role" in _check(role="admin")                  # 옛 역할은 못 만든다
    assert "region_ids" in _check(region_ids=[])
    assert "region_ids" in _check(region_ids=[3])          # 시·도가 아님
    assert _check(region_ids=[1, 2]) == {}                 # 여러 시·도
    assert _check(role=ac.ROLE_GUEST) == {}
    assert _check(role=ac.ROLE_SUPER, region_ids=None, super_count=2) == {}
    assert "role" in _check(role=ac.ROLE_SUPER, region_ids=None, super_count=3)   # 3명까지
    assert _check(password=None, username="gyeonggi1") == {}  # 비밀번호 안 볼 때


def test_scope_node_ids():
    tree = {1: [1, 10, 11, 100], 2: [2, 20]}
    assert ac.scope_node_ids(None, tree.get) is None
    assert ac.scope_node_ids([1], tree.get) == [1, 10, 11, 100]
    assert ac.scope_node_ids([1, 2], tree.get) == [1, 2, 10, 11, 20, 100]
    assert ac.scope_node_ids([9], tree.get) == [9]          # 모르는 id 는 자신만


def test_access_scope_helpers():
    tok = access._scope.set([10, 11])
    try:
        assert access.node_allowed(10) and not access.node_allowed(99)
        assert not access.node_allowed(None)
        assert access.device_allowed(11, "ACTIVE")
        assert not access.device_allowed(99, "PENDING")
        assert access.device_allowed(None, "PENDING")      # 지역 없는 승인 대기 — 뒤 6자리로 찾아 승인
        assert not access.device_allowed(None, "ACTIVE")
        with pytest.raises(Forbidden):
            access.check_node(99, action="t")
    finally:
        access._scope.reset(tok)
    assert access.current_scope() is None and access.node_allowed(None)


def test_guest_and_super_tables():
    assert any(p.match("/api/devices/map") for p in access.GUEST_ALLOW)
    assert not any(p.match("/api/commands") for p in access.GUEST_ALLOW)
    hit = [pat.pattern for m, pat in access.SUPER_ONLY
           if (m is None or "DELETE" in m) and pat.match("/api/devices/20363930594D50170004003A")]
    assert hit                                               # 단말 삭제는 최고관리자만
    assert not any(pat.match("/api/devices/20363930594D50170004003A/state")
                   for m, pat in access.SUPER_ONLY if m and "PATCH" in m)


def test_password_changed_rounds_up_to_next_second():
    t = dt.datetime(2026, 10, 4, 9, 0, 5, 300000, tzinfo=dt.timezone.utc)
    got = ac.password_changed_now(t)
    assert got == dt.datetime(2026, 10, 4, 9, 0, 6, tzinfo=dt.timezone.utc)
    assert int(t.timestamp()) < int(got.timestamp())  # 같은 초에 받은 옛 쿠키는 끊긴다
