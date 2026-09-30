"""/api/auth/* — 관리 화면 로그인(문제점 16번). 판정 규칙은 core/web_session.

    POST /api/auth/login   {user, password} → 쿠키 발급.
                           nginx 가 인증 없이 통과시키고 요청 수를 제한한다.
    POST /api/auth/logout  쿠키 지움.
    GET  /api/auth/check   nginx auth_request 전용 — 200 + X-Auth-User / 401. 본문 없음.

로그인한 뒤의 "나"는 기존 GET /api/me(X-Remote-User → 권한)를 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.core import web_session as ws
from app.errors import LoginFailed

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    user: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _accounts() -> dict[str, str]:
    return ws.accounts(settings.admin_user, settings.admin_password,
                       settings.operator_user, settings.operator_password)


def _key(accts: dict[str, str]) -> bytes:
    return ws.secret_key(settings.session_secret, accts)


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, str]:
    accts = _accounts()
    user = body.user.strip()
    if not ws.check_password(accts, user, body.password):
        await asyncio.sleep(1.0)  # 대입 시도 늦추기(nginx limit_req 와 함께)
        log.warning("관리 화면 로그인 실패: user=%s ip=%s", user[:64],
                    request.headers.get("X-Real-IP", "-"))
        raise LoginFailed()
    ttl = settings.session_hours * 3600
    response.set_cookie(
        ws.COOKIE, ws.issue(_key(accts), user, ttl), max_age=ttl, path="/", httponly=True,
        samesite="lax",
        secure=request.headers.get("X-Forwarded-Proto") == "https",
    )
    log.info("관리 화면 로그인: user=%s", user)
    return {"user": user}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(ws.COOKIE, path="/")
    return {"ok": True}


@router.get("/check")
async def check(request: Request) -> Response:
    accts = _accounts()
    user = ws.verify(_key(accts), request.cookies.get(ws.COOKIE), accts) \
        or ws.basic_user(request.headers.get("Authorization"), accts)
    if not user:
        return Response(status_code=401)
    return Response(status_code=200, headers={"X-Auth-User": user})
