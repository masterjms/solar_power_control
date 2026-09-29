"""스케줄 프로필 · 배정 · 배포(S-25, ADR-010, 사양서 §13.1).

배정: 단말 하나 배정이 먼저, 없으면 그 단말의 말단 법정동부터 위로 가장 가까운 노드 배정(상속).
배포 대상 = 범위 안 운영(ACTIVE) 단말 중 **지금 배정(상속 포함)이 그 프로필인 것**. 작업 하나 = 프로필 한 판.
진행은 mqtt/deploy_runner 가 한다. 여기서는 항목을 waiting 으로 만들고 러너를 깨운다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import presence
from app.core import schedule_rules as sr
from app.core import settings_rules as rules
from app.core.auth import Principal
from app.errors import (
    DeployJobNotFound,
    DeployNoTargets,
    DeviceNotFound,
    RegionNotFound,
    ScheduleProfileInUse,
    ScheduleProfileNameTaken,
    ScheduleProfileNotFound,
    ValidationFailed,
)
from app.models.device import Device
from app.models.schedule import (
    DeployItem,
    DeployJob,
    DeviceSchedule,
    ScheduleAssign,
    ScheduleProfile,
)
from app.models.settings import DeviceSettings
from app.modules.region.service import load_tree
from app.modules.schedule.schemas import (
    AssignIn,
    AssignOut,
    DeployIn,
    DeployItemOut,
    DeployJobOut,
    DeviceScheduleOut,
    DevicesPage,
    ProfileIn,
    ProfileOut,
    ProfilePatch,
    RetryOut,
)
from app.modules.settings.service import _raise as raise_settings
from app.mqtt.deploy_runner import OPEN, DeployRunner

log = logging.getLogger(__name__)

#: "응답 없는 단말만 다시" 로 되돌릴 수 있는 끝 상태.
RETRYABLE = ("NO_RESPONSE", "READ_FAILED", "FLASH", "CRC", "STATE", "RULE", "RANGE", "BAD")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ── 프로필 ───────────────────────────────────────────────────────────────
def _spec(region: Any, lat: Any, lon: Any, on: Any, off: Any,
          values: dict[str, Any]) -> tuple[rules.TableSpec, dict[str, int]]:
    try:
        table = rules.validate_table(region, lat, lon, on, off)
        clean = sr.validate_profile_values(values)
    except rules.SettingsInvalid as e:
        raise_settings(e)
    return table, clean


def _profile_out(p: ScheduleProfile, **counts: int) -> ProfileOut:
    return ProfileOut(
        id=p.id, name=p.name, version=p.version, region=p.region, lat_e6=p.lat_e6,
        lon_e6=p.lon_e6, on=p.on_corr, off=p.off_corr, crc=p.crc, values=p.values,
        address=p.address, updated_at=p.updated_at, updated_by=p.updated_by, **counts,
    )


async def _get_profile(db: AsyncSession, pid: int) -> ScheduleProfile:
    p = await db.get(ScheduleProfile, pid)
    if p is None:
        raise ScheduleProfileNotFound(detail={"id": pid})
    return p


async def create_profile(db: AsyncSession, body: ProfileIn, me: Principal) -> ProfileOut:
    table, values = _spec(body.region, body.lat, body.lon, body.on, body.off, body.values)
    p = ScheduleProfile(
        name=body.name.strip(), region=table.region, lat_e6=table.lat_e6, lon_e6=table.lon_e6,
        on_corr=table.on, off_corr=table.off, crc=table.crc, values=values,
        address=(body.address or "").strip() or None, updated_by=me.user,
    )
    db.add(p)
    try:
        await db.flush()
    except IntegrityError as e:
        raise ScheduleProfileNameTaken(detail={"name": body.name}) from e
    await db.refresh(p)
    return _profile_out(p)


async def patch_profile(db: AsyncSession, pid: int, body: ProfilePatch, me: Principal) -> ProfileOut:
    p = await _get_profile(db, pid)
    sent = body.model_dump(exclude_unset=True)
    region = sent.get("region", p.region)
    lat = sent.get("lat", p.lat_e6 / 1_000_000)
    lon = sent.get("lon", p.lon_e6 / 1_000_000)
    on = sent.get("on", p.on_corr)
    off = sent.get("off", p.off_corr)
    values = sent.get("values", p.values)
    table, clean = _spec(region, lat, lon, on, off, values)
    changed = (
        table.region != p.region or table.lat_e6 != p.lat_e6 or table.lon_e6 != p.lon_e6
        or table.on != p.on_corr or table.off != p.off_corr or clean != p.values
    )
    if "name" in sent and sent["name"]:
        p.name = sent["name"].strip()
    if "address" in sent:
        p.address = (sent["address"] or "").strip() or None
    if changed:
        p.region, p.lat_e6, p.lon_e6 = table.region, table.lat_e6, table.lon_e6
        p.on_corr, p.off_corr, p.crc, p.values = table.on, table.off, table.crc, clean
        p.version += 1
    p.updated_at = _now()
    p.updated_by = me.user
    try:
        await db.flush()
    except IntegrityError as e:
        raise ScheduleProfileNameTaken(detail={"name": sent.get("name")}) from e
    return _profile_out(p)


async def delete_profile(db: AsyncSession, pid: int) -> dict[str, Any]:
    p = await _get_profile(db, pid)
    n = await db.scalar(select(func.count()).where(ScheduleAssign.profile_id == pid)) or 0
    if n:
        raise ScheduleProfileInUse(detail={"id": pid, "assigned": int(n)})
    await db.delete(p)
    return {"id": pid, "deleted": True}


# ── 배정 · 상속 ─────────────────────────────────────────────────────────
class Coverage:
    """모든 단말의 (배정 프로필, 출처) 를 한 번에 푼다. 1만 대도 메모리에서 끝난다."""

    def __init__(self, tree, assigns: list[ScheduleAssign]) -> None:  # noqa: ANN001
        self.tree = tree
        self.by_node = {a.node_id: a.profile_id for a in assigns if a.node_id is not None}
        self.by_uuid = {a.uuid: a.profile_id for a in assigns if a.uuid is not None}

    def chain(self, node_id: int | None) -> list[int]:
        if node_id is None:
            return []
        return [n.id for n in reversed(self.tree.path(node_id))]

    def of(self, uuid: str, node_id: int | None) -> tuple[int | None, str | None]:
        return sr.effective_profile(self.by_uuid.get(uuid), self.chain(node_id), self.by_node)


async def coverage(db: AsyncSession) -> Coverage:
    assigns = list((await db.execute(select(ScheduleAssign))).scalars())
    return Coverage(await load_tree(db), assigns)


async def list_profiles(db: AsyncSession) -> list[ProfileOut]:
    profiles = list((await db.execute(select(ScheduleProfile).order_by(ScheduleProfile.name))).scalars())
    cov = await coverage(db)
    nodes = Counter(pid for pid in cov.by_node.values())
    devs = Counter(pid for pid in cov.by_uuid.values())
    rows = await db.execute(
        select(Device.uuid, Device.node_id, DeviceSchedule.profile_id,
               DeviceSchedule.profile_version, DeviceSchedule.crc, DeviceSettings.tbl_crc)
        .join(DeviceSchedule, DeviceSchedule.uuid == Device.uuid, isouter=True)
        .join(DeviceSettings, DeviceSettings.uuid == Device.uuid, isouter=True)
        .where(Device.state == "ACTIVE")
    )
    by_id = {p.id: p for p in profiles}
    targets: Counter = Counter()
    applied: Counter = Counter()
    for uuid, node_id, a_pid, a_ver, a_crc, dev_crc in rows:
        pid, _ = cov.of(uuid, node_id)
        if pid is None:
            continue
        targets[pid] += 1
        p = by_id.get(pid)
        if p and a_pid == pid and a_ver == p.version and a_crc == p.crc and \
                (dev_crc is None or dev_crc.strip().upper() == p.crc):
            applied[pid] += 1
    return [_profile_out(p, assigned_nodes=nodes[p.id], assigned_devices=devs[p.id],
                         targets=targets[p.id], applied=applied[p.id]) for p in profiles]


async def list_assign(db: AsyncSession) -> list[AssignOut]:
    tree = await load_tree(db)
    rows = await db.execute(
        select(ScheduleAssign, ScheduleProfile.name, Device.site)
        .join(ScheduleProfile, ScheduleProfile.id == ScheduleAssign.profile_id)
        .join(Device, Device.uuid == ScheduleAssign.uuid, isouter=True)
        .order_by(ScheduleAssign.id)
    )
    out = []
    for a, pname, site in rows:
        label = tree.path_name(a.node_id) if a.node_id is not None else (site or a.uuid)
        out.append(AssignOut(id=a.id, node_id=a.node_id, uuid=a.uuid, profile_id=a.profile_id,
                             profile_name=pname, label=label, assigned_at=a.assigned_at,
                             assigned_by=a.assigned_by))
    return out


async def set_assign(db: AsyncSession, body: AssignIn, me: Principal) -> dict[str, Any]:
    if (body.node_id is None) == (body.uuid is None):
        raise ValidationFailed("node_id 와 uuid 중 하나만 준다", detail={"node_id": body.node_id,
                                                                      "uuid": body.uuid})
    uuid = body.uuid.strip().upper() if body.uuid else None
    if body.node_id is not None:
        tree = await load_tree(db)
        if tree.get(body.node_id) is None:
            raise RegionNotFound(detail={"id": body.node_id})
    elif await db.get(Device, uuid) is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    cond = (ScheduleAssign.node_id == body.node_id) if body.node_id is not None \
        else (ScheduleAssign.uuid == uuid)
    cur = (await db.execute(select(ScheduleAssign).where(cond))).scalar_one_or_none()
    if body.profile_id is None:
        if cur is not None:
            await db.delete(cur)
        return {"node_id": body.node_id, "uuid": uuid, "profile_id": None}
    await _get_profile(db, body.profile_id)
    if cur is None:
        db.add(ScheduleAssign(node_id=body.node_id, uuid=uuid, profile_id=body.profile_id,
                              assigned_by=me.user))
    else:
        cur.profile_id = body.profile_id
        cur.assigned_at = _now()
        cur.assigned_by = me.user
    return {"node_id": body.node_id, "uuid": uuid, "profile_id": body.profile_id}


# ── 단말별 스케줄 상태 ─────────────────────────────────────────────────
def _today(p: ScheduleProfile | None) -> tuple[str | None, str | None]:
    if p is None:
        return None, None
    now = _now().astimezone(dt.timezone(dt.timedelta(hours=9)))
    row = rules.schedule_preview_day(p.lat_e6, p.lon_e6, p.on_corr, p.off_corr, now.month, now.day)
    return row


async def devices(
    db: AsyncSession, *, node_id: int | None, profile_id: int | None, q: str | None,
    page: int, size: int, uuid: str | None = None,
) -> DevicesPage:
    now = _now()
    cov = await coverage(db)
    profiles = {p.id: p for p in (await db.execute(select(ScheduleProfile))).scalars()}
    online = presence.online_clause(now).label("online_now")
    stmt = (
        select(Device.uuid, Device.site, Device.state, Device.node_id, online, DeviceSchedule,
               DeviceSettings.tbl_crc, DeviceSettings.tbl_region, DeviceSettings.tbl_src,
               DeviceSettings.dip)
        .join(DeviceSchedule, DeviceSchedule.uuid == Device.uuid, isouter=True)
        .join(DeviceSettings, DeviceSettings.uuid == Device.uuid, isouter=True)
    )
    if uuid is not None:
        stmt = stmt.where(Device.uuid == uuid)
    else:
        stmt = stmt.where(Device.state == "ACTIVE")
    if node_id is not None:
        if cov.tree.get(node_id) is None:
            raise RegionNotFound(detail={"id": node_id})
        stmt = stmt.where(Device.node_id.in_(cov.tree.subtree_ids(node_id) or [-1]))
    if q and q.strip():
        needle = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(Device.uuid.ilike(needle, escape="\\") | Device.site.ilike(needle, escape="\\"))
    rows = (await db.execute(stmt.order_by(Device.site.nulls_last(), Device.uuid))).all()
    open_items = {
        u: (st, j) for u, st, j in await db.execute(
            select(DeployItem.uuid, DeployItem.status, DeployItem.job_id)
            .join(DeployJob, DeployJob.id == DeployItem.job_id)
            .where(DeployItem.status.in_(OPEN), DeployJob.cancelled_at.is_(None))
        )
    }
    out: list[DeviceScheduleOut] = []
    for u, site, state, nid, is_on, applied, dev_crc, dev_region, dev_src, dip in rows:
        pid, source = cov.of(u, nid)
        if profile_id is not None and pid != profile_id:
            continue
        p = profiles.get(pid) if pid is not None else None
        ok = bool(p and applied and applied.profile_id == p.id and applied.profile_version == p.version
                  and applied.crc == p.crc and (dev_crc is None or dev_crc.strip().upper() == p.crc))
        t_on, t_off = _today(p)
        st = open_items.get(u)
        out.append(DeviceScheduleOut(
            uuid=u, site=site, state=state, is_online=bool(is_on),
            node_path=cov.tree.path_name(nid) if nid else None,
            profile_id=pid, profile_name=p.name if p else None,
            profile_version=p.version if p else None, profile_crc=p.crc if p else None,
            source=source,
            applied_profile_id=applied.profile_id if applied else None,
            applied_version=applied.profile_version if applied else None,
            applied_crc=applied.crc if applied else None,
            applied_at=applied.applied_at if applied else None,
            device_crc=dev_crc.strip().upper() if dev_crc else None, device_region=dev_region,
            device_src=dev_src, applied_ok=ok,
            deploy_status=st[0] if st else None, deploy_job_id=st[1] if st else None,
            dip4=None if dip is None else bool(dip & 0x08), today_on=t_on, today_off=t_off,
        ))
    total = len(out)
    return DevicesPage(items=out[(page - 1) * size:page * size], total=total, page=page, size=size)


async def device_schedule(db: AsyncSession, uuid: str) -> DeviceScheduleOut:
    page = await devices(db, node_id=None, profile_id=None, q=None, page=1, size=1, uuid=uuid)
    if not page.items:
        raise DeviceNotFound(detail={"uuid": uuid})
    return page.items[0]


# ── 배포 ─────────────────────────────────────────────────────────────────
async def create_deploy(
    db: AsyncSession, body: DeployIn, me: Principal, runner: DeployRunner | None,
) -> DeployJobOut:
    p = await _get_profile(db, body.profile_id)
    cov = await coverage(db)
    stmt = select(Device.uuid, Device.node_id).where(Device.state == "ACTIVE")
    label: str | None = p.name
    if body.scope == "node":
        try:
            nid = int(str(body.scope_id))
        except ValueError as e:
            raise ValidationFailed("scope_id = 노드 id", detail={"scope_id": body.scope_id}) from e
        if cov.tree.get(nid) is None:
            raise RegionNotFound(detail={"id": nid})
        stmt = stmt.where(Device.node_id.in_(cov.tree.subtree_ids(nid) or [-1]))
        label = cov.tree.path_name(nid)
    elif body.scope == "device":
        uuid = str(body.scope_id or "").strip().upper()
        stmt = stmt.where(Device.uuid == uuid)
        label = uuid
    targets = [u for u, nid in await db.execute(stmt) if cov.of(u, nid)[0] == p.id]
    if not targets:
        raise DeployNoTargets(detail={"profile_id": p.id, "scope": body.scope,
                                      "scope_id": body.scope_id})
    now = _now()
    # 같은 단말의 이전 배포(진행 중)는 새 작업으로 대체한다 — 이미 보낸 요청은 끝까지 가지만 결과는 무시.
    await db.execute(
        update(DeployItem)
        .where(DeployItem.uuid.in_(targets), DeployItem.status.in_(OPEN))
        .values(status="SUPERSEDED", detail="새 배포로 대체", updated_at=now)
    )
    job = DeployJob(
        profile_id=p.id, profile_name=p.name, profile_version=p.version, crc=p.crc,
        spec={"tbl": {"region": p.region, "lat_e6": p.lat_e6, "lon_e6": p.lon_e6,
                      "on": p.on_corr, "off": p.off_corr, "crc": p.crc},
              "values": dict(p.values)},
        scope_kind=body.scope, scope_id=body.scope_id, scope_label=label, total=len(targets),
        created_by=me.user, created_at=now,
    )
    db.add(job)
    await db.flush()
    db.add_all([DeployItem(job_id=job.id, uuid=u, status="waiting", updated_at=now)
                for u in targets])
    await db.commit()
    log.info("배포 #%d 프로필 %s v%d → %d대 (%s %s, by %s)", job.id, p.name, p.version,
             len(targets), body.scope, body.scope_id, me.user)
    if runner is not None:
        runner.mark(targets)
        await runner.process(targets)
    return await get_job(db, job.id, items=False)


async def _counts(db: AsyncSession, job_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {j: {} for j in job_ids}
    if not job_ids:
        return out
    for j, st, n in await db.execute(
        select(DeployItem.job_id, DeployItem.status, func.count())
        .where(DeployItem.job_id.in_(job_ids)).group_by(DeployItem.job_id, DeployItem.status)
    ):
        out[j][st] = int(n)
    return out


def _job_out(j: DeployJob, counts: dict[str, int], items: list[DeployItemOut] | None) -> DeployJobOut:
    return DeployJobOut(
        id=j.id, profile_id=j.profile_id, profile_name=j.profile_name,
        profile_version=j.profile_version, crc=j.crc, scope_kind=j.scope_kind,
        scope_id=j.scope_id, scope_label=j.scope_label, total=j.total, created_by=j.created_by,
        created_at=j.created_at, finished_at=j.finished_at, cancelled_at=j.cancelled_at,
        counts=counts, items=items,
    )


async def list_jobs(db: AsyncSession, *, limit: int, profile_id: int | None,
                    uuid: str | None) -> list[DeployJobOut]:
    stmt = select(DeployJob).order_by(DeployJob.id.desc()).limit(limit)
    if profile_id is not None:
        stmt = stmt.where(DeployJob.profile_id == profile_id)
    if uuid is not None:
        stmt = stmt.where(DeployJob.id.in_(select(DeployItem.job_id).where(DeployItem.uuid == uuid)))
    jobs = list((await db.execute(stmt)).scalars())
    counts = await _counts(db, [j.id for j in jobs])
    return [_job_out(j, counts[j.id], None) for j in jobs]


async def get_job(db: AsyncSession, job_id: int, *, items: bool = True) -> DeployJobOut:
    j = await db.get(DeployJob, job_id, populate_existing=True)
    if j is None:
        raise DeployJobNotFound(detail={"id": job_id})
    out_items: list[DeployItemOut] | None = None
    if items:
        now = _now()
        online = presence.online_clause(now).label("online_now")
        rows = await db.execute(
            select(DeployItem, Device.site, online)
            .join(Device, Device.uuid == DeployItem.uuid, isouter=True)
            .where(DeployItem.job_id == job_id).order_by(Device.site.nulls_last(), DeployItem.uuid)
        )
        out_items = [DeployItemOut(uuid=i.uuid, site=site, is_online=bool(on), status=i.status,
                                   rounds=i.rounds, sent_at=i.sent_at, acked_at=i.acked_at,
                                   detail=i.detail) for i, site, on in rows]
    counts = await _counts(db, [job_id])
    return _job_out(j, counts[job_id], out_items)


async def retry_job(db: AsyncSession, job_id: int, uuids: list[str] | None,
                    runner: DeployRunner | None, max_rounds: int) -> RetryOut:
    j = await db.get(DeployJob, job_id)
    if j is None:
        raise DeployJobNotFound(detail={"id": job_id})
    stmt = select(DeployItem).where(DeployItem.job_id == job_id,
                                    DeployItem.status.in_(RETRYABLE))
    if uuids:
        stmt = stmt.where(DeployItem.uuid.in_([u.strip().upper() for u in uuids]))
    items = list((await db.execute(stmt.with_for_update())).scalars())
    now = _now()
    retried = [i for i in items if i.rounds < max_rounds]
    for i in retried:
        i.status, i.detail, i.updated_at = "waiting", None, now
    if retried:
        j.finished_at = None
        j.cancelled_at = None
    await db.commit()
    if runner is not None and retried:
        runner.mark([i.uuid for i in retried])
        await runner.process([i.uuid for i in retried])
    return RetryOut(retried=len(retried), skipped=len(items) - len(retried))


async def cancel_job(db: AsyncSession, job_id: int) -> DeployJobOut:
    j = await db.get(DeployJob, job_id)
    if j is None:
        raise DeployJobNotFound(detail={"id": job_id})
    now = _now()
    await db.execute(
        update(DeployItem).where(DeployItem.job_id == job_id, DeployItem.status.in_(OPEN))
        .values(status="CANCELLED", detail="관리자 취소", updated_at=now)
    )
    j.cancelled_at = j.cancelled_at or now
    j.finished_at = j.finished_at or now
    await db.commit()
    return await get_job(db, job_id, items=False)
