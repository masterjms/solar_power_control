"""관리자 식별 — 로그인한 사용자명(`X-Remote-User`) → 역할·맡은 시·도 (ADR-005 권한, ADR-013 계정).

nginx 가 로그인 판정(/api/auth/check)을 통과한 사용자명을 `X-Remote-User` 헤더로 넘긴다. 백엔드 포트는
운영에서 호스트에 노출되지 않으므로(컨테이너 네트워크 안에서 nginx 만 닿는다) 이 헤더를 믿는다.

    SUPER_ADMIN_USERS(쉼표, 기본 admin) 에 있으면   super_admin — 전 기능(.env 계정)
    그 밖의 .env 계정(OPERATOR_USER)                  admin       — 전 지역, 최고관리자 아님(옛 방식)
    DB 계정(admin_user, 화면에서 만듦)                그 계정의 역할·시·도 (만료·사용 중지면 401)
    헤더 없음 + APP_ENV=dev                           super_admin `local` (개발 PC 에서 8000 직접)
    헤더 없음 + 그 외 / 모르는 이름                   admin (권한을 올려 주지 않는다 — 시험 도구 호환)

판정 규칙의 순수 부분은 resolve_principal, DB 를 보는 것은 current_user.
명령 이력의 "누가"(command.created_by) 가 이 user 다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select

from app.config import settings
from app.core import accounts as ac
from app.errors import Forbidden, SessionEnded

ROLE_SUPER = ac.ROLE_SUPER
ROLE_ADMIN = ac.ROLE_ADMIN
HEADER = "X-Remote-User"


@dataclass(frozen=True)
class Principal:
    user: str
    role: str
    #: 맡은 시·도(트리 최상위 id). None = 전 지역.
    region_ids: tuple[int, ...] | None = None
    #: env / db / dev
    source: str = "env"
    expires_at: dt.datetime | None = None

    @property
    def is_super(self) -> bool:
        return self.role == ROLE_SUPER


def resolve_principal(
    header: str | None, *, super_users: frozenset[str] | set[str], app_env: str
) -> Principal:
    """.env 규칙만으로 판정(DB 계정은 current_user 가 덧씌운다)."""
    user = (header or "").strip()
    if not user:
        # 개발 PC 에서 nginx 없이 8000 으로 바로 부를 때만 최고관리자로 본다. 운영에서 헤더가
        # 빠졌다면 프록시 설정 사고이므로 권한을 올려 주지 않는다.
        if app_env == "dev":
            return Principal("local", ROLE_SUPER, source="dev")
        return Principal("anonymous", ROLE_ADMIN)
    return Principal(user, ROLE_SUPER if user in super_users else ROLE_ADMIN)


def env_usernames() -> set[str]:
    """.env 로 만든 계정 이름(화면에서 같은 이름을 못 만든다)."""
    out = {u for u in (settings.admin_user, settings.operator_user) if u}
    return out | set(settings.super_admin_users)


async def current_user(request: Request) -> Principal:
    """FastAPI 의존성. 한 요청에 한 번만 판정한다(request.state.principal)."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    header = request.headers.get(HEADER)
    p = resolve_principal(header, super_users=settings.super_admin_users, app_env=settings.app_env)
    name = (header or "").strip()
    if name and name not in env_usernames():
        db = getattr(request.state, "db", None)
        if db is not None:
            from app.models.system import AdminUser  # 순환 import 회피

            row = await db.scalar(select(AdminUser).where(AdminUser.username == name))
            if row is not None:
                now = dt.datetime.now(dt.timezone.utc)
                if not ac.is_valid(disabled=row.disabled, expires_at=row.expires_at, now=now):
                    raise SessionEnded(detail={"user": name})
                regions = None if row.role == ac.ROLE_SUPER else tuple(row.region_ids or ())
                p = Principal(name, row.role, regions, "db", row.expires_at)
    request.state.principal = p
    return p


def require_super(principal: Principal, *, action: str) -> None:
    """최고관리자 전용 동작 앞에서 부른다. 아니면 403 FORBIDDEN."""
    if not principal.is_super:
        raise Forbidden(detail={"user": principal.user, "role": principal.role, "action": action})
