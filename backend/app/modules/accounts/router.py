"""/api/accounts — 계정 관리(문제점 21번, ADR-013). 규칙은 core/accounts.

최고관리자만(access_guard 의 SUPER_ONLY 가 아니라 여기서 require_super — me/password 는 누구나):
  GET    /api/accounts                 .env 계정(읽기 전용) + DB 계정, 만료 7일 안 표시
  POST   /api/accounts                 {username, password, role, region_ids, expires}
  PATCH  /api/accounts/{id}            {role?, region_ids?, expires?, disabled?}
  POST   /api/accounts/{id}/password   {password} — 재설정(그 계정의 기존 로그인 끊김)
  DELETE /api/accounts/{id}
  GET    /api/accounts/logins?page=&size=   로그인 기록 {items,total,page,size}(문제점 39번)
누구나(DB 계정만):
  POST   /api/accounts/me/password     {old, new}
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import accounts as ac
from app.core.auth import Principal, current_user, env_usernames, require_super
from app.db import get_db
from app.errors import AccountInvalid, AccountNotFound, Forbidden, LoginFailed
from app.models.region import Region
from app.models.system import AdminUser, LoginLog

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/accounts", tags=["accounts"])

EXPIRING_DAYS = 7


class AccountIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: str
    region_ids: list[int] | None = None
    expires: str = "never"


class AccountPatch(BaseModel):
    role: str | None = None
    region_ids: list[int] | None = None
    #: 사용 기간을 지금부터 다시(7d…never)
    expires: str | None = None
    disabled: bool | None = None


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class MyPasswordIn(BaseModel):
    old: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=1, max_length=256)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _top_regions(db: AsyncSession) -> dict[int, str]:
    rows = await db.execute(select(Region.id, Region.name).where(Region.parent_id.is_(None)))
    return {int(i): n for i, n in rows}


async def _super_count(db: AsyncSession, *, exclude_id: int | None = None) -> int:
    env_supers = {u for u in settings.super_admin_users if u == settings.admin_user}
    q = select(func.count()).select_from(AdminUser).where(AdminUser.role == ac.ROLE_SUPER)
    if exclude_id is not None:
        q = q.where(AdminUser.id != exclude_id)
    return len(env_supers) + int(await db.scalar(q) or 0)


def _out(a: AdminUser, tops: dict[int, str], now: dt.datetime) -> dict[str, Any]:
    exp = a.expires_at
    return {
        "id": a.id, "username": a.username, "role": a.role, "role_label": ac.ROLE_LABEL.get(a.role, a.role),
        "region_ids": a.region_ids, "regions": [tops.get(i, f"#{i}") for i in (a.region_ids or [])],
        "expires_at": exp, "expired": exp is not None and exp <= now,
        "expiring": exp is not None and now < exp <= now + dt.timedelta(days=EXPIRING_DAYS),
        "disabled": a.disabled, "created_by": a.created_by, "created_at": a.created_at,
        "last_login_at": a.last_login_at, "source": "db",
    }


def _env_rows() -> list[dict[str, Any]]:
    out = []
    for name in (settings.admin_user, settings.operator_user):
        if not name:
            continue
        role = ac.ROLE_SUPER if name in settings.super_admin_users else ac.ROLE_ADMIN
        out.append({"id": None, "username": name, "role": role, "role_label": ac.ROLE_LABEL[role],
                    "region_ids": None, "regions": [], "expires_at": None, "expired": False,
                    "expiring": False, "disabled": False, "created_by": ".env", "created_at": None,
                    "last_login_at": None, "source": "env"})
    return out


@router.get("")
async def list_accounts(me: Principal = Depends(current_user),
                        db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    require_super(me, action="accounts.list")
    tops = await _top_regions(db)
    now = _now()
    rows = (await db.execute(select(AdminUser).order_by(AdminUser.role, AdminUser.username))).scalars()
    items = _env_rows() + [_out(a, tops, now) for a in rows]
    return {"items": items, "regions": [{"id": i, "name": n} for i, n in sorted(tops.items(), key=lambda x: x[1])],
            "max_super": ac.MAX_SUPER, "super_count": await _super_count(db),
            "expiry_choices": list(ac.EXPIRY_DAYS)}


@router.post("", status_code=201)
async def create_account(body: AccountIn, me: Principal = Depends(current_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    require_super(me, action="accounts.create")
    username = body.username.strip()
    tops = await _top_regions(db)
    errors = ac.check_new_account(
        username=username, password=body.password, role=body.role,
        region_ids=body.region_ids, reserved=env_usernames(), top_ids=tops,
        super_count=await _super_count(db))
    if await db.scalar(select(AdminUser.id).where(AdminUser.username == username)):
        errors["username"] = "이미 있는 이름"
    try:
        expires = ac.expires_from(body.expires, _now())
    except ValueError:
        errors["expires"] = "7d·15d·30d·90d·180d·365d·never 중 하나"
        expires = None
    if errors:
        raise AccountInvalid(detail={"fields": errors})
    a = AdminUser(username=username, password_hash=ac.hash_password(body.password), role=body.role,
                  region_ids=None if body.role == ac.ROLE_SUPER else sorted(set(body.region_ids or [])),
                  expires_at=expires, created_by=me.user)
    db.add(a)
    await db.flush()
    log.info("계정 만듦: %s(%s) by %s", username, body.role, me.user)
    return _out(a, tops, _now())


@router.post("/me/password")
async def my_password(body: MyPasswordIn, me: Principal = Depends(current_user),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """자기 비밀번호 바꾸기 — 화면에서 만든 계정만(.env 계정은 서버 .env 에서)."""
    a = await db.scalar(select(AdminUser).where(AdminUser.username == me.user))
    if a is None:
        raise Forbidden(".env 계정의 비밀번호는 서버 .env 에서 바꿉니다.", code="ENV_ACCOUNT")
    if not ac.verify_password(body.old, a.password_hash):
        raise LoginFailed("지금 비밀번호가 맞지 않습니다.", code="BAD_PASSWORD")
    if len(body.new) < ac.PASSWORD_MIN:
        raise AccountInvalid(detail={"fields": {"new": f"{ac.PASSWORD_MIN}자 이상"}})
    a.password_hash = ac.hash_password(body.new)
    a.password_changed_at = ac.password_changed_now(_now())
    return {"ok": True, "relogin": True}


async def _get(db: AsyncSession, account_id: int) -> AdminUser:
    a = await db.get(AdminUser, account_id)
    if a is None:
        raise AccountNotFound(detail={"id": account_id})
    return a


@router.patch("/{account_id}")
async def patch_account(account_id: int, body: AccountPatch, me: Principal = Depends(current_user),
                        db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    require_super(me, action="accounts.patch")
    a = await _get(db, account_id)
    tops = await _top_regions(db)
    role = body.role or a.role
    regions = body.region_ids if body.region_ids is not None else a.region_ids
    errors = ac.check_new_account(
        username=a.username, password=None, role=role, region_ids=regions, reserved=(),
        top_ids=tops, super_count=await _super_count(db, exclude_id=a.id))
    expires = a.expires_at
    if body.expires is not None:
        try:
            expires = ac.expires_from(body.expires, _now())
        except ValueError:
            errors["expires"] = "7d·15d·30d·90d·180d·365d·never 중 하나"
    if a.username == me.user and (role != ac.ROLE_SUPER or body.disabled):
        errors["role"] = "자기 자신의 최고관리자 권한은 내리거나 중지할 수 없다"
    if errors:
        raise AccountInvalid(detail={"fields": errors})
    a.role = role
    a.region_ids = None if role == ac.ROLE_SUPER else sorted(set(regions or []))
    a.expires_at = expires
    if body.disabled is not None:
        a.disabled = body.disabled
    await db.flush()
    log.info("계정 바꿈: %s → %s %s 만료 %s 중지 %s by %s", a.username, a.role, a.region_ids,
             a.expires_at, a.disabled, me.user)
    return _out(a, tops, _now())


@router.post("/{account_id}/password")
async def reset_password(account_id: int, body: PasswordIn, me: Principal = Depends(current_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    require_super(me, action="accounts.password")
    a = await _get(db, account_id)
    if len(body.password) < ac.PASSWORD_MIN:
        raise AccountInvalid(detail={"fields": {"password": f"{ac.PASSWORD_MIN}자 이상"}})
    a.password_hash = ac.hash_password(body.password)
    a.password_changed_at = ac.password_changed_now(_now())
    log.info("비밀번호 재설정: %s by %s", a.username, me.user)
    return {"ok": True}


@router.delete("/{account_id}")
async def delete_account(account_id: int, me: Principal = Depends(current_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    require_super(me, action="accounts.delete")
    a = await _get(db, account_id)
    if a.username == me.user:
        raise Forbidden("자기 계정은 지울 수 없습니다.", code="SELF_DELETE")
    await db.delete(a)
    log.warning("계정 지움: %s by %s", a.username, me.user)
    return {"deleted": a.username}


@router.get("/logins")
async def logins(page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
                 me: Principal = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    require_super(me, action="accounts.logins")
    total = int(await db.scalar(select(func.count()).select_from(LoginLog)) or 0)
    rows = (await db.execute(select(LoginLog).order_by(LoginLog.id.desc())
                             .offset((page - 1) * size).limit(size))).scalars()
    return {"items": [{"at": r.at, "username": r.username, "ok": r.ok, "reason": r.reason, "ip": r.ip}
                      for r in rows], "total": total, "page": page, "size": size}
