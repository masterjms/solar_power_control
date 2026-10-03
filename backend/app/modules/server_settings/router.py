"""/api/server-settings — 운영 중 바꾸는 서버 설정(문제점 14번, ADR-012). 최고관리자 전용.

항목 정의·범위·기본값은 core/server_settings.ITEMS. 화면은 GET 이 주는 정의대로 그린다(항목이 늘어도 화면 수정 없음).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import server_settings as ss
from app.core.auth import Principal, current_user, require_super
from app.db import get_db
from app.errors import ValidationFailed
from app.models.system import ServerSetting

log = logging.getLogger(__name__)
router = APIRouter(tags=["server-settings"])


class SettingsIn(BaseModel):
    values: dict[str, Any]


async def load(db: AsyncSession) -> int:
    """DB → 메모리. 기동 때 부른다. 읽은 행 수."""
    rows = (await db.execute(select(ServerSetting.key, ServerSetting.value))).all()
    ss.runtime.apply({k: v for k, v in rows})
    return len(rows)


async def _view(db: AsyncSession) -> dict[str, Any]:
    meta = {r.key: r for r in (await db.execute(select(ServerSetting))).scalars()}
    groups = []
    for gid, title in ss.GROUPS:
        items = []
        for it in ss.ITEMS:
            if it.group != gid:
                continue
            row = meta.get(it.key)
            items.append({
                "key": it.key, "label": it.label, "help": it.help, "unit": it.unit,
                "min": it.min, "max": it.max, "default": it.default, "scale": it.scale, "kind": it.kind, "scale": it.scale, "kind": it.kind,
                "value": ss.runtime.get(it.key),
                "updated_by": row.updated_by if row else None,
                "updated_at": row.updated_at if row else None,
            })
        groups.append({"id": gid, "title": title, "items": items})
    return {"groups": groups}


@router.get("/api/server-settings")
async def get_settings(
    me: Principal = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    require_super(me, action="server_settings.read")
    return await _view(db)


@router.put("/api/server-settings")
async def put_settings(
    body: SettingsIn, me: Principal = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """보낸 항목만 바꾼다. 하나라도 범위 밖이면 아무것도 바꾸지 않고 422."""
    require_super(me, action="server_settings.write")
    clean, errors = ss.validate(body.values)
    if errors:
        raise ValidationFailed("범위를 벗어난 값이 있습니다.", detail={"fields": errors})
    now = dt.datetime.now(dt.timezone.utc)
    for key, value in clean.items():
        before = ss.runtime.get(key)
        stmt = pg_insert(ServerSetting).values(key=key, value=value, updated_by=me.user,
                                               updated_at=now)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=[ServerSetting.key],
            set_={"value": value, "updated_by": me.user, "updated_at": now},
        ))
        if before != value:
            log.info("서버 설정 변경: %s %s → %s (by %s)", key, before, value, me.user)
    await db.commit()
    ss.runtime.update(clean)
    return await _view(db)
