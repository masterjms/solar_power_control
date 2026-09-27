"""관리자 식별 — nginx Basic auth 사용자명(`X-Remote-User`) → (user, role) (ADR-005 권한).

로그인 기능(4차 전)이 붙기 전까지의 방식이다. nginx(web 컨테이너)가 Basic auth 를 통과한
사용자명을 `X-Remote-User` 헤더로 넘긴다. 백엔드 포트는 운영에서 호스트에 노출되지 않으므로
(컨테이너 네트워크 안에서 nginx 만 닿는다) 이 헤더를 믿는다.

    SUPER_ADMIN_USERS(쉼표, 기본 admin) 에 있으면 super_admin — 전체 명령, 트리 편집
    그 외                                          admin       — 개별·노드 명령, 조회
    헤더 없음 + APP_ENV=dev                         super_admin `local` (개발 PC 에서 8000 직접)
    헤더 없음 + 그 외                               admin `anonymous` (권한을 올려 주지 않는다)

판정은 순수 함수(resolve_principal)이고, FastAPI 의존성(current_user)은 그것을 부를 뿐이다.
명령 이력의 "누가"(command.created_by) 가 이 user 다.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.config import settings
from app.errors import Forbidden

ROLE_SUPER = "super_admin"
ROLE_ADMIN = "admin"
HEADER = "X-Remote-User"


@dataclass(frozen=True)
class Principal:
    user: str
    role: str

    @property
    def is_super(self) -> bool:
        return self.role == ROLE_SUPER


def resolve_principal(
    header: str | None, *, super_users: frozenset[str] | set[str], app_env: str
) -> Principal:
    user = (header or "").strip()
    if not user:
        # 개발 PC 에서 nginx 없이 8000 으로 바로 부를 때만 최고관리자로 본다. 운영에서 헤더가
        # 빠졌다면 프록시 설정 사고이므로 권한을 올려 주지 않는다.
        if app_env == "dev":
            return Principal("local", ROLE_SUPER)
        return Principal("anonymous", ROLE_ADMIN)
    return Principal(user, ROLE_SUPER if user in super_users else ROLE_ADMIN)


async def current_user(request: Request) -> Principal:
    """FastAPI 의존성. 요청마다 헤더를 다시 본다(세션 없음)."""
    return resolve_principal(
        request.headers.get(HEADER),
        super_users=settings.super_admin_users,
        app_env=settings.app_env,
    )


def require_super(principal: Principal, *, action: str) -> None:
    """최고관리자 전용 동작 앞에서 부른다. 아니면 403 FORBIDDEN."""
    if not principal.is_super:
        raise Forbidden(detail={"user": principal.user, "role": principal.role, "action": action})
