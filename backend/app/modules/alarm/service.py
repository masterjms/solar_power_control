"""/api/alarms — 알람 목록·이력(ADR-009). 관찰 행(opened_at NULL)은 보이지 않는다."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import alarm_rules as ar
from app.models.alarm import Alarm
from app.models.device import Device
from app.modules.alarm.schemas import AlarmOut, AlarmPage
from app.modules.region.service import load_tree


def _esc(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _base(status: str, tab: str | None, kind: str | None, q: str | None,
          uuid: str | None = None) -> Select:
    stmt = (
        select(Alarm, Device.site, Device.address, Device.node_id, Device.state)
        .join(Device, Device.uuid == Alarm.uuid, isouter=True)
        .where(Alarm.opened_at.is_not(None))
    )
    stmt = stmt.where(Alarm.closed_at.is_(None) if status == "open"
                      else Alarm.closed_at.is_not(None))
    if tab:
        stmt = stmt.where(Alarm.kind.in_([k for k, v in ar.KINDS.items() if v.tab == tab]))
    if kind:
        stmt = stmt.where(Alarm.kind == kind.upper())
    if uuid:
        stmt = stmt.where(Alarm.uuid == uuid)
    if q and q.strip():
        needle = _esc(q.strip())
        stmt = stmt.where(Alarm.uuid.ilike(needle, escape="\\")
                          | Device.site.ilike(needle, escape="\\")
                          | Device.address.ilike(needle, escape="\\"))
    return stmt


def _order(stmt: Select, status: str) -> Select:
    if status != "open":
        return stmt.order_by(Alarm.closed_at.desc(), Alarm.id.desc())
    rank = case(*[(Alarm.severity == s, r) for s, r in ar.SEVERITY_RANK.items()], else_=9)
    return stmt.order_by(rank, Alarm.first_seen_at.desc(), Alarm.id.desc())


def _out(row, now: dt.datetime, tree) -> AlarmOut:  # noqa: ANN001
    a, site, address, node_id, state = row
    kind = ar.KINDS.get(a.kind)
    end = a.closed_at or now
    return AlarmOut(
        id=a.id, uuid=a.uuid, kind=a.kind, label=kind.label if kind else a.kind,
        tab=kind.tab if kind else None, severity=a.severity, first_seen_at=a.first_seen_at,
        opened_at=a.opened_at, last_seen_at=a.last_seen_at, closed_at=a.closed_at,
        duration_sec=int((end - a.first_seen_at).total_seconds()), value=a.value,
        site=site, address=address, state=state,
        node_path=tree.path_name(node_id) if tree is not None and node_id else None,
    )


async def counts(db: AsyncSession) -> dict[str, int]:
    """열린 알람 탭별 건수(+ all). 필터와 무관."""
    out = {t: 0 for t in ar.TABS}
    rows = await db.execute(
        select(Alarm.kind, func.count())
        .where(Alarm.closed_at.is_(None), Alarm.opened_at.is_not(None))
        .group_by(Alarm.kind)
    )
    for kind, n in rows:
        tab = ar.tab_of(kind)
        if tab:
            out[tab] += int(n)
    out["all"] = sum(out[t] for t in ar.TABS)
    return out


async def list_alarms(
    db: AsyncSession, *, status: str, tab: str | None, kind: str | None, q: str | None,
    page: int, size: int,
) -> AlarmPage:
    now = dt.datetime.now(dt.timezone.utc)
    base = _base(status, tab, kind, q)
    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (await db.execute(_order(base, status).offset((page - 1) * size).limit(size))).all()
    tree = await load_tree(db)
    return AlarmPage(items=[_out(r, now, tree) for r in rows], total=int(total), page=page,
                     size=size, counts=await counts(db))


async def device_alarms(db: AsyncSession, uuid: str, limit: int) -> list[AlarmOut]:
    """그 단말의 열린 알람 + 최근 이력."""
    now = dt.datetime.now(dt.timezone.utc)
    tree = await load_tree(db)
    open_rows = (await db.execute(_order(_base("open", None, None, None, uuid), "open"))).all()
    closed = (await db.execute(
        _order(_base("closed", None, None, None, uuid), "closed").limit(limit)
    )).all()
    return [_out(r, now, tree) for r in [*open_rows, *closed]]
