"""관리 화면 로그인 세션(문제점 16번) — core/web_session 순수 함수."""

import base64

from app.core import web_session as ws

ACCTS = ws.accounts("admin", "pw-admin", "op1", "pw-op")
KEY = ws.secret_key("", ACCTS)


def test_accounts_skip_empty_and_sample():
    assert ws.accounts("admin", "", "", "") == {}
    assert ws.accounts("admin", "change-me-admin", "", "") == {}
    assert ws.accounts("admin", "a", "op", "") == {"admin": "a"}
    # 같은 이름은 추가하지 않는다
    assert ws.accounts("admin", "a", "admin", "b") == {"admin": "a"}
    assert ACCTS == {"admin": "pw-admin", "op1": "pw-op"}


def test_password_check():
    assert ws.check_password(ACCTS, "admin", "pw-admin")
    assert not ws.check_password(ACCTS, "admin", "pw-op")
    assert not ws.check_password(ACCTS, "nobody", "pw-admin")
    assert not ws.check_password(ACCTS, "nobody", "\0")


def test_issue_verify_roundtrip_and_expiry():
    tok = ws.issue(KEY, "op1", 3600, now=1000)
    assert ws.verify(KEY, tok, ACCTS, now=2000) == "op1"
    assert ws.verify(KEY, tok, ACCTS, now=1000 + 3601) is None  # 만료


def test_verify_rejects_forgery():
    tok = ws.issue(KEY, "op1", 3600, now=1000)
    user, exp, sig = tok.split(".")
    assert ws.verify(KEY, f"admin.{exp}.{sig}", ACCTS, now=1500) is None  # 사용자 바꿔치기
    # 만료 늘리기
    assert ws.verify(KEY, f"{user}.{int(exp) + 99999}.{sig}", ACCTS, now=1500) is None
    assert ws.verify(b"other", tok, ACCTS, now=1500) is None
    for bad in (None, "", "a.b", "a.x.y", "a.1.2.3"):
        assert ws.verify(KEY, bad, ACCTS, now=1500) is None


def test_password_change_invalidates_sessions():
    tok = ws.issue(KEY, "admin", 3600, now=1000)
    new = ws.accounts("admin", "new-pw", "op1", "pw-op")
    assert ws.verify(ws.secret_key("", new), tok, new, now=1500) is None
    # 명시 키면 비밀번호와 무관하게 유지, 단 지워진 계정은 거부
    k = ws.secret_key("fixed", ACCTS)
    t2 = ws.issue(k, "op1", 3600, now=1000)
    assert ws.verify(ws.secret_key("fixed", new), t2, new, now=1500) == "op1"
    assert ws.verify(k, t2, {"admin": "x"}, now=1500) is None


def test_basic_header_for_scripts():
    h = "Basic " + base64.b64encode(b"admin:pw-admin").decode()
    assert ws.basic_user(h, ACCTS) == "admin"
    assert ws.basic_user("Basic " + base64.b64encode(b"admin:bad").decode(), ACCTS) is None
    assert ws.basic_user("Basic !!!", ACCTS) is None
    assert ws.basic_user("Bearer x", ACCTS) is None
    assert ws.basic_user(None, ACCTS) is None
