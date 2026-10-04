"""/api/energy — 대시보드 "발전과 사용" 그래프 · "에너지 합계" (문제점 29번), 통계 다시 시작(27번).

자료: 날짜별은 `telemetry_daily`(단말이 보낸 eg·eu 로 적은 gen_wh/use_wh — 옛 펌웨어는 서버 추정
pp_wh 를 "추정"으로), 오늘은 `device.last_telemetry` 의 eg·eu 합, 누적은 `device.energy_*_wh_total`
(일 집계가 매일 다시 센다) + 오늘. 통계 시작일(서버 설정 stats_since) 이전은 넣지 않는다.
폐기된 단말의 과거분도 지역 합계에 남긴다(단말측 결정 ④).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import energy
from app.core import server_settings as ss
from app.core.access import current_scope
from app.core.auth import Principal, current_user, require_super
from app.db import get_db
from app.models.device import Device
from app.models.system import ServerSetting
from app.models.telemetry import TelemetryDaily
from app.modules.region.service import load_tree
from app.tasks.daily_rollup import KST

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/energy", tags=["energy"])

PERIODS = {"7d": 7, "30d": 30, "12m": 365}


async def _scope(db: AsyncSession, node_id: int | None) -> list[int] | None:
    """node_id 아래(자신 포함) 지역 id 목록. None = 전체."""
    scope = current_scope()
    if node_id is None:
        return scope
    tree = await load_tree(db)
    ids = tree.subtree_ids(node_id) or [node_id]
    return ids if scope is None else [i for i in ids if i in set(scope)]


@router.get("/summary")
async def summary(
    period: str = Query(default="7d", pattern="^(7d|30d|12m)$"),
    node_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    today_start = energy.today_start_kst(now)
    today = today_start.date()
    since = ss.runtime.stats_since
    days = PERIODS[period]
    if period == "12m":
        frm = (today.replace(day=1) - dt.timedelta(days=334)).replace(day=1)
    else:
        frm = today - dt.timedelta(days=days - 1)
    if since and since > frm:
        frm = since
    node_ids = await _scope(db, node_id)

    # ── 날짜별(오늘 전) ──
    bucket = func.date_trunc("month", TelemetryDaily.day) if period == "12m" else TelemetryDaily.day
    q = (
        select(bucket.label("b"), func.sum(TelemetryDaily.gen_wh), func.sum(TelemetryDaily.use_wh),
               func.sum(func.coalesce(TelemetryDaily.pp_wh, 0))
                   .filter(TelemetryDaily.gen_wh.is_(None)),
               func.count(func.distinct(TelemetryDaily.uuid)))
        .where(TelemetryDaily.day >= frm, TelemetryDaily.day < today)
    )
    if node_ids is not None:
        q = q.where(TelemetryDaily.uuid.in_(
            select(Device.uuid).where(Device.node_id.in_(node_ids))))
    q = q.group_by(bucket).order_by(bucket)
    rows = {(r[0].date() if isinstance(r[0], dt.datetime) else r[0]): r
            for r in await db.execute(q)}

    # ── 오늘 + 누적(단말 행) ──
    dq = select(Device.uuid, Device.last_telemetry, Device.last_telemetry_at,
                Device.energy_gen_wh_total, Device.energy_use_wh_total)
    if node_ids is not None:
        dq = dq.where(Device.node_id.in_(node_ids))
    devs = (await db.execute(dq)).all()
    tod = energy.aggregate_today([(tm, at) for _, tm, at, _, _ in devs], today_start)
    gen_total = sum(int(g or 0) for *_, g, _ in devs) + tod["gen_wh"]
    use_total = sum(int(u or 0) for *_, _, u in devs) + tod["use_wh"]
    if since and since > today:
        gen_total = use_total = 0

    # ── 막대 목록(빈 날은 0, 통계 시작일 전은 뺀다) ──
    out: list[dict[str, Any]] = []
    if period == "12m":
        m = frm.replace(day=1)
        while m <= today:
            r = rows.get(m)
            out.append({"day": m.isoformat(), "gen_wh": int(r[1] or 0) if r else 0,
                        "use_wh": int(r[2] or 0) if r else 0,
                        "est_gen_wh": int(r[3] or 0) if r else 0,
                        "devices": int(r[4]) if r else 0, "today": m == today.replace(day=1)})
            m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        if out and out[-1]["today"]:
            out[-1]["gen_wh"] += tod["gen_wh"]
            out[-1]["use_wh"] += tod["use_wh"]
    else:
        d = frm
        while d < today:
            r = rows.get(d)
            out.append({"day": d.isoformat(), "gen_wh": int(r[1] or 0) if r else 0,
                        "use_wh": int(r[2] or 0) if r else 0,
                        "est_gen_wh": int(r[3] or 0) if r else 0,
                        "devices": int(r[4]) if r else 0, "today": False})
            d += dt.timedelta(days=1)
        if not since or since <= today:
            out.append({"day": today.isoformat(), "gen_wh": tod["gen_wh"], "use_wh": tod["use_wh"],
                        "est_gen_wh": 0, "devices": tod["reported"], "today": True})

    ghg = ss.runtime.ghg_kg_per_kwh
    return {
        "period": period, "since": since.isoformat() if since else None, "from": frm.isoformat(),
        "today": today.isoformat(), "node_id": node_id, "days": out,
        "now": {**tod, "total_devices": len(devs)},
        "total": {"gen_wh": gen_total, "use_wh": use_total,
                  "co2_kg": round(gen_total / 1000 * ghg, 3)},
        "ghg_kg_per_kwh": ghg,
    }


@router.post("/reset")
async def reset(
    me: Principal = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """통계 다시 시작(문제점 27번) — 통계 시작일 = 오늘(KST), 누적 0, 오늘 전 하루 요약 삭제.
    10분 보고 원문은 보관 기간 규칙대로 따로 지워진다. 최고관리자만."""
    require_super(me, action="energy.reset")
    today = dt.datetime.now(KST).date()
    val = ss.int_of(today)
    now = dt.datetime.now(dt.timezone.utc)
    stmt = pg_insert(ServerSetting).values(key="stats_since", value=val, updated_by=me.user,
                                           updated_at=now)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[ServerSetting.key],
        set_={"value": val, "updated_by": me.user, "updated_at": now}))
    deleted = (await db.execute(text("DELETE FROM telemetry_daily WHERE day < :d"),
                                {"d": today})).rowcount
    zeroed = (await db.execute(update(Device).values(energy_gen_wh_total=0,
                                                     energy_use_wh_total=0))).rowcount
    await db.commit()
    ss.runtime.update({"stats_since": val})
    log.warning("통계 다시 시작 by %s: 시작일 %s, 하루 요약 %d행 삭제, 단말 %d대 누적 0",
                me.user, today, deleted, zeroed)
    return {"since": today.isoformat(), "daily_rows_deleted": int(deleted or 0),
            "devices_zeroed": int(zeroed or 0)}
