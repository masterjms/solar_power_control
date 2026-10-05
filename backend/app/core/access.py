"""접근 검사 한 곳 — 역할·지역 범위(문제점 21번, ADR-013).

모든 API 라우터에 `access_guard` 를 의존성으로 건다(app/main.py). 요청마다:
  1. 누구인가(core/auth.current_user) — 역할·맡은 시·도.
  2. 지역 범위 계산 → contextvar `_scope` 에 둔다. None = 전 지역(최고관리자·옛 관리자·개발 PC),
     list = 볼 수 있는 트리 노드 id(맡은 시·도 아래 전체). 목록 조회 서비스는 `in_scope(컬럼)` 으로 거른다.
  3. 역할별 막기:
     · 게스트 — GUEST_ALLOW 의 GET 만(대시보드에 필요한 것). 나머지 403 GUEST_READ_ONLY.
     · 최고관리자 전용 — SUPER_ONLY 표(서버 설정·서버 상태·계정·통신 주기 설정 쓰기·지역 이름/삭제·단말 삭제…).
  4. 경로에 `{uuid}` 가 있으면 그 단말이 범위 안인지 — 아니면 404(있다는 것도 알리지 않는다).
     예외: 지역이 없는 승인 대기 단말은 지역관리자도 uuid 로 연다(뒤 6자리 검색 → 승인, 단말측 결정).
본문에 노드·단말이 들어오는 쓰기(명령·지정·보내기·지역 추가·단말 지역 바꾸기)는 서비스에서
`check_node` / `check_device` 로 본다.

스케줄러·MQTT 처리(요청 밖)는 contextvar 가 기본값 None 이라 전 지역이다.
"""

from __future__ import annotations

import contextvars
import re

from fastapi import Depends, Request
from sqlalchemy import ColumnElement, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import accounts as ac
from app.core.auth import Principal, current_user
from app.errors import DeviceNotFound, Forbidden

_scope: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("scope", default=None)

#: 게스트가 부를 수 있는 것(GET) — 대시보드·로그인 정보.
GUEST_ALLOW = (
    re.compile(r"^/api/me$"), re.compile(r"^/api/ui-config$"), re.compile(r"^/health$"),
    re.compile(r"^/api/devices$"), re.compile(r"^/api/devices/map$"),
    re.compile(r"^/api/devices/energy$"), re.compile(r"^/api/energy/summary$"),
    re.compile(r"^/api/alarms$"), re.compile(r"^/api/regions$"), re.compile(r"^/api/activity$"),
)
#: 게스트도 되는 쓰기 — 자기 비밀번호.
GUEST_WRITE_ALLOW = (re.compile(r"^/api/accounts/me/password$"),)

#: 최고관리자만 — (메서드 집합 또는 None=전부, 경로 정규식).
SUPER_ONLY: tuple[tuple[frozenset[str] | None, re.Pattern[str]], ...] = (
    (None, re.compile(r"^/api/server-settings")),
    (None, re.compile(r"^/api/admin/")),
    (None, re.compile(r"^/api/system/")),
    (None, re.compile(r"^/api/metrics$")),
    (frozenset({"POST"}), re.compile(r"^/api/energy/reset$")),
    (frozenset({"POST", "PATCH", "DELETE"}), re.compile(r"^/api/profiles")),
    (frozenset({"PATCH", "DELETE"}), re.compile(r"^/api/regions/\d+$")),
    (frozenset({"DELETE"}), re.compile(r"^/api/devices/[0-9A-Fa-f]{24}$")),
)


def current_scope() -> list[int] | None:
    """지금 요청의 지역 범위. None = 전 지역."""
    return _scope.get()


def in_scope(col) -> ColumnElement[bool]:  # noqa: ANN001
    """목록 조회에 붙이는 조건. 전 지역이면 참."""
    scope = _scope.get()
    if scope is None:
        return true()
    return col.in_(scope or [-1])


def node_allowed(node_id: int | None) -> bool:
    scope = _scope.get()
    return scope is None or (node_id is not None and node_id in scope)


def check_node(node_id: int | None, *, action: str) -> None:
    """본문의 노드가 범위 밖이면 403."""
    if not node_allowed(node_id):
        raise Forbidden("맡은 지역 밖입니다.", code="OUT_OF_REGION",
                        detail={"node_id": node_id, "action": action})


def device_allowed(node_id: int | None, state: str | None) -> bool:
    """단말 하나를 다뤄도 되나 — 범위 안이거나, 지역이 없는 승인 대기 단말(뒤 6자리로 찾아 승인)."""
    if node_allowed(node_id):
        return True
    return node_id is None and state == "PENDING"


async def _compute_scope(db: AsyncSession, me: Principal) -> list[int] | None:
    if me.region_ids is None:
        return None
    from app.modules.region.service import load_tree  # 순환 import 회피

    tree = await load_tree(db)
    return ac.scope_node_ids(me.region_ids, tree.subtree_ids)


def _path_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


async def access_guard(request: Request, me: Principal = Depends(current_user)) -> None:
    """모든 API 라우터의 공통 의존성. 위 머리말 참고."""
    db: AsyncSession = request.state.db
    path, method = request.url.path, request.method.upper()

    if me.role == ac.ROLE_GUEST:
        allowed = (method == "GET" and any(p.match(path) for p in GUEST_ALLOW)) \
            or (method == "POST" and any(p.match(path) for p in GUEST_WRITE_ALLOW))
        if not allowed:
            raise Forbidden("게스트는 대시보드만 볼 수 있습니다.", code="GUEST_READ_ONLY",
                            detail={"path": path})
    if not me.is_super:
        for methods, pat in SUPER_ONLY:
            if (methods is None or method in methods) and pat.match(path):
                raise Forbidden(detail={"user": me.user, "role": me.role, "action": path})

    _scope.set(await _compute_scope(db, me))

    uuid = request.path_params.get("uuid")
    if uuid and _scope.get() is not None:
        from app.models.device import Device

        dev = await db.get(Device, str(uuid).strip().upper())
        if dev is None or not device_allowed(dev.node_id, dev.state):
            raise DeviceNotFound(detail={"uuid": str(uuid).strip().upper()})
