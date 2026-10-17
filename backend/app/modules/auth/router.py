"""/api/auth/* — 관리 화면 로그인(문제점 16번, 21번). 판정 규칙은 core/web_session·core/accounts.

    POST /api/auth/login   {user, password} → 쿠키 발급.
                           nginx 가 인증 없이 통과시키고 요청 수를 제한한다.
    POST /api/auth/logout  쿠키 지움.
    GET  /api/auth/check   nginx auth_request 전용 — 200 + X-Auth-User / 401. 본문 없음.

계정: .env(ADMIN_USER·OPERATOR_USER) 먼저, 없으면 DB(admin_user — 화면에서 만든 계정). DB 계정은
만료·사용 중지면 로그인·쿠키 모두 거부, 비밀번호를 바꾼 뒤에는 그 전에 받은 쿠키를 끊는다.
로그인 성공·실패는 login_log 에 남는다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import accounts as ac
from app.core import web_session as ws
from app.db import get_db
from app.errors import LoginFailed
from app.models.system import AdminUser, LoginLog

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    user: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    #: 로그인 유지 — 기본 켬(예전 동작). 끄면 브라우저를 닫을 때 로그아웃.
    remember: bool = True


def _accounts() -> dict[str, str]:
    return ws.accounts(settings.admin_user, settings.admin_password,
                       settings.operator_user, settings.operator_password)


def _key(accts: dict[str, str]) -> bytes:
    return ws.secret_key(settings.session_secret, accts)


async def _lock_until(db: AsyncSession, user: str, now: dt.datetime) -> dt.datetime | None:
    rows = (await db.execute(
        select(LoginLog.at, LoginLog.ok, LoginLog.reason)
        .where(LoginLog.username == user[:64], LoginLog.at > now - dt.timedelta(minutes=ac.LOCK_MINUTES))
        .order_by(LoginLog.at.desc()).limit(50))).all()
    return ac.lock_until(rows, now)


def _locked_msg(until: dt.datetime) -> str:
    kst = until.astimezone(dt.timezone(dt.timedelta(hours=9)))
    return (f"비밀번호를 {ac.LOCK_FAILS}번 연속 틀려 {ac.LOCK_MINUTES}분 동안 잠겼습니다 — "
            f"{kst:%H:%M} 이후 다시 하거나 최고관리자에게 잠금 해제를 요청하세요.")


async def _log(db: AsyncSession, user: str, ok: bool, reason: str, request: Request) -> None:
    db.add(LoginLog(username=user[:64], ok=ok, reason=reason,
                    ip=request.headers.get("X-Real-IP") or (request.client.host if request.client else None)))
    await db.commit()


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    accts = _accounts()
    user = body.user.strip()
    now = dt.datetime.now(dt.timezone.utc)
    # 연속 실패 잠금(문제점 54번) — 잠긴 동안은 비밀번호를 보지 않는다
    until = await _lock_until(db, user, now)
    if until is not None:
        await _log(db, user, False, "locked", request)
        raise LoginFailed(_locked_msg(until), code="ACCOUNT_LOCKED")
    ok = ws.check_password(accts, user, body.password)
    if not ok and user not in accts:
        row = await db.scalar(select(AdminUser).where(AdminUser.username == user))
        if row is not None and ac.verify_password(body.password, row.password_hash):
            if row.disabled:
                await _log(db, user, False, "disabled", request)
                raise LoginFailed("사용 중지된 계정입니다 — 최고관리자에게 문의하세요.",
                                  code="ACCOUNT_DISABLED")
            if row.expires_at is not None and row.expires_at <= now:
                await _log(db, user, False, "expired", request)
                raise LoginFailed("계정 사용 기간이 끝났습니다 — 최고관리자에게 연장을 요청하세요.",
                                  code="ACCOUNT_EXPIRED")
            row.last_login_at = now
            ok = True
    if not ok:
        await _log(db, user, False, "bad_password", request)
        await asyncio.sleep(1.0)  # 대입 시도 늦추기(nginx limit_req 와 함께)
        log.warning("관리 화면 로그인 실패: user=%s ip=%s", user[:64],
                    request.headers.get("X-Real-IP", "-"))
        until = await _lock_until(db, user, now)
        if until is not None:
            raise LoginFailed(_locked_msg(until), code="ACCOUNT_LOCKED")
        raise LoginFailed()
    await _log(db, user, True, "ok", request)
    # 로그인 유지(문제점 25번 로그인 화면): 켜면 SESSION_HOURS 동안 쿠키 유지, 끄면 브라우저를 닫을 때 사라지는 쿠키.
    # 토큰 수명은 둘 다 SESSION_HOURS — session_user 가 발급 시각을 '만료 − SESSION_HOURS' 로 계산하므로 바꾸면 안 된다
    # (10/14 에 12시간으로 줄였다가 화면에서 만든 계정이 로그인 직후 끊기던 버그, 10/17 수정).
    # 오래 켜 둔 화면은 무동작 자동 로그아웃(서버 설정 idle_logout_min)이 끊는다.
    ttl = settings.session_hours * 3600
    response.set_cookie(
        ws.COOKIE, ws.issue(_key(accts), user, ttl), max_age=ttl if body.remember else None, path="/", httponly=True,
        samesite="lax",
        secure=request.headers.get("X-Forwarded-Proto") == "https",
    )
    log.info("관리 화면 로그인: user=%s", user)
    return {"user": user}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(ws.COOKIE, path="/")
    return {"ok": True}


async def session_user(db: AsyncSession, token: str | None) -> str | None:
    """쿠키 → 지금도 유효한 사용자명. .env 계정이거나, DB 계정이 만료·중지 전이고 비밀번호를 바꾼 뒤 발급된 것."""
    accts = _accounts()
    got = ws.parse(_key(accts), token)
    if got is None:
        return None
    user, exp = got
    if user in accts:
        return user
    row = await db.scalar(select(AdminUser).where(AdminUser.username == user))
    now = dt.datetime.now(dt.timezone.utc)
    if row is None or not ac.is_valid(disabled=row.disabled, expires_at=row.expires_at, now=now):
        return None
    # 쿠키는 초 단위 — 바꾼 시각은 다음 정수 초로 올려 두었으므로(accounts.mark_password_changed) 엄격히 비교한다.
    issued = exp - settings.session_hours * 3600
    if issued < int(row.password_changed_at.timestamp()):
        return None
    return user


@router.get("/check")
async def check(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    user = await session_user(db, request.cookies.get(ws.COOKIE)) \
        or ws.basic_user(request.headers.get("Authorization"), _accounts())
    if not user:
        return Response(status_code=401)
    return Response(status_code=200, headers={"X-Auth-User": user})
