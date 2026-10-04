"""관리 화면 로그인 세션(문제점 16번) — Basic auth 창 대신 로그인 화면 + 서명 쿠키. ADR-011.

스마트폰, 특히 메신저 앱 안의 브라우저(카카오톡 등)는 HTTP Basic auth 입력 창을
띄우지 않고 바로 "401 Authorization Required" 를 보여 준다. 그래서 nginx 는
`auth_request` 로 이 모듈의 판정(/api/auth/check)을 묻고, 통과한 사용자명을 지금처럼
`X-Remote-User` 로 백엔드에 넘긴다(ADR-005 권한 판정은 그대로).

계정은 그대로 `.env` 의 ADMIN_USER/ADMIN_PASSWORD(+ 선택 OPERATOR_*).
스크립트(healthcheck.sh 등)가 쓰는 `Authorization: Basic` 도 계속 받는다 — 판정 순서: 쿠키 → Basic.

쿠키 = "<사용자>.<만료 unix초>.<HMAC-SHA256 hex>" (사용자명에 '.' 이 없을 때만 발급).
서명 키 = SESSION_SECRET, 비우면 계정 비밀번호에서 유도 — 비밀번호를 바꾸면 세션이 모두 끊긴다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

COOKIE = "iotl_session"


def accounts(
    admin_user: str, admin_password: str, operator_user: str, operator_password: str
) -> dict[str, str]:
    """사용할 수 있는 계정 {사용자: 비밀번호}.

    비밀번호가 빈 계정·견본 값은 넣지 않는다(nginx 30-htpasswd.sh 와 같은 규칙)."""
    out: dict[str, str] = {}
    if admin_user and admin_password and admin_password != "change-me-admin":
        out[admin_user] = admin_password
    if operator_user and operator_password and operator_user != admin_user:
        out[operator_user] = operator_password
    return out


def secret_key(explicit: str, accts: dict[str, str]) -> bytes:
    if explicit:
        return explicit.encode()
    seed = "\n".join(f"{u}:{p}" for u, p in sorted(accts.items()))
    return hashlib.sha256(b"iotlight-web-session\n" + seed.encode()).digest()


def check_password(accts: dict[str, str], user: str, password: str) -> bool:
    want = accts.get(user)
    # 없는 사용자도 같은 비교를 한 번 해서 응답 시간으로 사용자 유무가 드러나지 않게 한다.
    return hmac.compare_digest((want or "\0").encode(), password.encode()) and want is not None


def _sig(key: bytes, user: str, exp: int) -> str:
    return hmac.new(key, f"{user}.{exp}".encode(), hashlib.sha256).hexdigest()


def issue(key: bytes, user: str, ttl_sec: int, now: float | None = None) -> str:
    if "." in user:
        raise ValueError("사용자명에 '.' 을 쓸 수 없다")
    exp = int((now if now is not None else time.time()) + ttl_sec)
    return f"{user}.{exp}.{_sig(key, user, exp)}"


def parse(key: bytes, token: str | None, now: float | None = None) -> tuple[str, int] | None:
    """서명이 맞고 만료 전이면 (사용자명, 만료 unix초). 계정이 아직 있는지는 부르는 쪽이 본다."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    user, exp_s, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if not hmac.compare_digest(_sig(key, user, exp), sig):
        return None
    if exp < (now if now is not None else time.time()):
        return None
    return user, exp


def verify(
    key: bytes, token: str | None, accts: dict[str, str], now: float | None = None
) -> str | None:
    """.env 계정용 — 유효하면 사용자명. 만료·위조·지금은 없는 계정이면 None."""
    got = parse(key, token, now)
    if got is None or got[0] not in accts:
        return None
    return got[0]


def basic_user(header: str | None, accts: dict[str, str]) -> str | None:
    """`Authorization: Basic …` 가 맞으면 사용자명."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    user, sep, password = raw.partition(":")
    if not sep:
        return None
    return user if check_password(accts, user, password) else None
