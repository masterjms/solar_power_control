"""/api/ota/* — OTA 관리(문제점 56번, 사양서 §24). 최고관리자만(access_guard SUPER_ONLY).

  GET    /api/ota/overview                 진행 설정·지금 시간대·열린 항목 수
  GET    /api/ota/packages                 올린 펌웨어 목록
  POST   /api/ota/packages                 multipart file(.bin) + version + note → pkg(base64 한 줄)·sha256 계산
  DELETE /api/ota/packages/{id}            진행 중인 작업이 쓰면 409
  GET    /api/ota/devices?node_id=|uuid=   대상 고르기용 단말 목록(펌웨어·온라인·마지막 OTA)
  POST   /api/ota/jobs                     {package_id, kind: device|node, id}
  GET    /api/ota/jobs?page=&size=         보낸 기록(상태별 수)
  GET    /api/ota/jobs/{id}                단말별 결과
  POST   /api/ota/jobs/{id}/cancel         아직 시작 안 한(대기) 단말만 취소 — 받는 중인 단말은 멈출 수 없다
  POST   /api/ota/jobs/{id}/retry          실패·응답 없음 단말만 새 작업으로 다시
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import UUID_RE, DeviceState
from app.core import ota_rules as orl
from app.core import presence
from app.core.auth import Principal, current_user
from app.core.server_settings import ota_window_open
from app.core.server_settings import runtime as runtime_settings
from app.db import get_db
from app.errors import (
    Conflict,
    DeviceNotFound,
    NodeNotLeaf,
    NotFound,
    RegionNotFound,
    ValidationFailed,
)
from app.models.device import Device
from app.models.ota import OtaItem, OtaJob, OtaPackage
from app.modules.region.service import load_tree
from app.mqtt.ota_runner import OtaRunner

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ota", tags=["ota"])

#: "실패·응답 없음만 다시"에 넣는 끝 상태(성공·취소·운영 아님은 뺀다).
RETRY_STATUSES = ("ROLLBACK", "BAD", "STATE", "BATT", "BUSY", "HTTP", "SIZE", "HASH", "FLASH", "NO_RESPONSE")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _runner(request: Request) -> OtaRunner | None:
    return getattr(request.app.state, "ota_runner", None)


def _pkg_out(p: OtaPackage, used: int = 0, done: int = 0) -> dict[str, Any]:
    return {"id": p.id, "filename": p.filename, "version": p.version, "note": p.note,
            "fw_size": p.fw_size, "size": p.size, "sha256": p.sha256, "created_by": p.created_by,
            "created_at": p.created_at, "jobs": used, "done": done}


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    now = _now()
    start_h, end_h = runtime_settings.get("ota_hour_start"), runtime_settings.get("ota_hour_end")
    rows = await db.execute(select(OtaItem.status, func.count()).where(OtaItem.status.in_(orl.OPEN))
                            .group_by(OtaItem.status))
    open_counts = {s: 0 for s in orl.OPEN} | {s: int(n) for s, n in rows}
    return {"parallel": runtime_settings.get("ota_parallel"), "hour_start": start_h, "hour_end": end_h,
            "window_open": ota_window_open(now, start_h, end_h), "open": open_counts,
            "base_url": settings.ota_base_url, "chunk": orl.CHUNK, "fw_max": orl.FW_MAX}


# ── 펌웨어 ─────────────────────────────────────────────────────────────
@router.get("/packages")
async def list_packages(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    pkgs = (await db.execute(select(OtaPackage).order_by(OtaPackage.id.desc()))).scalars().all()
    used = dict((await db.execute(select(OtaJob.package_id, func.count()).group_by(OtaJob.package_id))).all())
    done = dict((await db.execute(
        select(OtaJob.package_id, func.count()).join(OtaItem, OtaItem.job_id == OtaJob.id)
        .where(OtaItem.status == "DONE").group_by(OtaJob.package_id))).all())
    return [_pkg_out(p, int(used.get(p.id, 0)), int(done.get(p.id, 0))) for p in pkgs]


@router.post("/packages", status_code=status.HTTP_201_CREATED)
async def upload_package(file: UploadFile = File(...), version: str = Form(""), note: str = Form(""),
                         me: Principal = Depends(current_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """펌웨어 bin 을 받아 pkg(base64 한 줄)로 바꿔 저장한다. sha256·size 는 pkg 기준(사양서 §24.2)."""
    fw = await file.read(orl.FW_MAX + 1)
    try:
        pkg = orl.make_pkg(fw)
    except orl.PackageInvalid as e:
        raise ValidationFailed(str(e), code="OTA_FILE_INVALID", detail={"fields": {"file": str(e)}}) from e
    same = await db.scalar(select(OtaPackage).where(OtaPackage.sha256 == pkg.sha256))
    if same is not None:
        raise Conflict(f"같은 파일이 이미 있습니다(#{same.id} {same.filename}).", code="OTA_FILE_EXISTS",
                       detail={"id": same.id})
    p = OtaPackage(filename=(file.filename or "firmware.bin")[:120], version=version.strip()[:40] or None,
                   note=note.strip()[:200] or None, fw_size=len(fw), size=pkg.size, sha256=pkg.sha256,
                   data=pkg.data, created_by=me.user)
    db.add(p)
    await db.flush()
    log.info("OTA 펌웨어 올림 #%d %s %dB → pkg %dB sha256 %s by %s", p.id, p.filename, len(fw), pkg.size,
             pkg.sha256[:12], me.user)
    return _pkg_out(p)


@router.delete("/packages/{pid}")
async def delete_package(pid: int, me: Principal = Depends(current_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    p = await db.get(OtaPackage, pid)
    if p is None:
        raise NotFound("없는 펌웨어입니다.", code="OTA_PACKAGE_NOT_FOUND")
    busy = await db.scalar(select(func.count()).select_from(OtaItem).join(OtaJob, OtaJob.id == OtaItem.job_id)
                           .where(OtaJob.package_id == pid, OtaItem.status.in_(orl.OPEN)))
    if busy:
        raise Conflict(f"이 파일로 대기·진행 중인 단말이 {busy}대 있습니다 — 끝난 뒤 지우세요.", code="OTA_PACKAGE_IN_USE")
    await db.delete(p)
    log.info("OTA 펌웨어 지움 #%d %s by %s", pid, p.filename, me.user)
    return {"deleted": pid}


# ── 대상 ─────────────────────────────────────────────────────────────
@router.get("/devices")
async def devices(node_id: int | None = Query(None), uuid: str | None = Query(None, max_length=24),
                  db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """대상 고르기 — 법정동 하나의 단말, 또는 uuid 하나. 마지막 OTA 결과를 같이 준다."""
    now = _now()
    stmt = select(Device.uuid, Device.site, Device.fw, Device.state,
                  presence.online_clause(now).label("online_now")).order_by(Device.site, Device.uuid)
    if uuid:
        stmt = stmt.where(Device.uuid == uuid.strip().upper())
    elif node_id is not None:
        stmt = stmt.where(Device.node_id == node_id)
    else:
        return {"items": []}
    rows = (await db.execute(stmt.limit(1000))).all()
    uuids = [r.uuid for r in rows]
    last: dict[str, Any] = {}
    if uuids:
        latest = (select(OtaItem.uuid, func.max(OtaItem.id).label("id"))
                  .where(OtaItem.uuid.in_(uuids)).group_by(OtaItem.uuid).subquery())
        for it in (await db.execute(select(OtaItem).join(latest, latest.c.id == OtaItem.id))).scalars():
            last[it.uuid] = {"item_id": it.id, "job_id": it.job_id, "status": it.status,
                             "at": it.finished_at or it.started_at or it.sent_at or it.updated_at}
    return {"items": [{"uuid": r.uuid, "site": r.site, "fw": r.fw, "state": r.state, "online": bool(r.online_now),
                       "last_ota": last.get(r.uuid)} for r in rows]}


class JobIn(BaseModel):
    package_id: int
    kind: str = Field(pattern="^(device|node)$")
    id: str = Field(min_length=1, max_length=40)


async def _create_job(db: AsyncSession, pkg: OtaPackage, *, kind: str, scope_id: str | None, label: str,
                      uuids: list[str], me: Principal) -> dict[str, Any]:
    """열린 OTA 가 있는 단말은 뺀다(한 단말에 하나씩). 남은 게 없으면 409."""
    busy = {r[0] for r in await db.execute(
        select(OtaItem.uuid).where(OtaItem.uuid.in_(uuids or ["-"]), OtaItem.status.in_(orl.OPEN)))}
    targets = [u for u in uuids if u not in busy]
    if not targets:
        raise Conflict("보낼 단말이 없습니다" + (" — 모두 이미 OTA 대기·진행 중" if busy else " — 운영(승인) 단말 없음"),
                       code="OTA_NO_TARGETS", detail={"busy": sorted(busy)})
    job = OtaJob(package_id=pkg.id, filename=pkg.filename, version=pkg.version, size=pkg.size,
                 sha256=pkg.sha256, scope_kind=kind, scope_id=scope_id, scope_label=label,
                 total=len(targets), created_by=me.user)
    db.add(job)
    await db.flush()
    db.add_all([OtaItem(job_id=job.id, uuid=u) for u in targets])
    await db.flush()
    log.info("OTA 작업 #%d %s %s %d대(빠짐 %d) 파일 #%d by %s", job.id, kind, label, len(targets), len(busy),
             pkg.id, me.user)
    return {"id": job.id, "total": len(targets), "skipped_busy": sorted(busy)}


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(body: JobIn, request: Request, me: Principal = Depends(current_user),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """단말 하나 또는 **법정동(말단) 하나**. 시·구 같은 상위 노드 일괄은 막는다(사양서 §24.5).
    운영(ACTIVE) 단말만 넣는다. 오프라인 단말은 대기로 남았다가 접속하면 시작한다."""
    pkg = await db.get(OtaPackage, body.package_id)
    if pkg is None:
        raise NotFound("없는 펌웨어입니다.", code="OTA_PACKAGE_NOT_FOUND")
    if body.kind == "device":
        uuid = body.id.strip().upper()
        if not UUID_RE.match(uuid):
            raise ValidationFailed("단말 UUID 는 24자리 16진수", detail={"field": "id"})
        d = await db.get(Device, uuid)
        if d is None:
            raise DeviceNotFound(detail={"uuid": uuid})
        if d.state != DeviceState.ACTIVE.value:
            raise Conflict("운영(승인) 단말에만 보낼 수 있습니다.", code="OTA_NOT_ACTIVE")
        label = f"{d.site} ({uuid[-6:]})" if d.site else uuid
        out = await _create_job(db, pkg, kind="device", scope_id=uuid, label=label, uuids=[uuid], me=me)
    else:
        try:
            node_id = int(body.id)
        except ValueError as e:
            raise ValidationFailed("법정동 id(숫자)", detail={"field": "id"}) from e
        tree = await load_tree(db)
        node = tree.get(node_id)
        if node is None:
            raise RegionNotFound(detail={"id": node_id})
        if not node.is_leaf:
            raise NodeNotLeaf("OTA 는 단말 하나 또는 법정동(말단) 하나씩 보냅니다 — 시·구 일괄은 막혀 있습니다.")
        uuids = [r[0] for r in await db.execute(
            select(Device.uuid).where(Device.node_id == node_id, Device.state == DeviceState.ACTIVE.value)
            .order_by(Device.uuid))]
        out = await _create_job(db, pkg, kind="node", scope_id=str(node_id),
                                label=tree.path_name(node_id) or node.name, uuids=uuids, me=me)
    if (r := _runner(request)) is not None:
        r.wake()
    return out


# ── 보낸 기록 ─────────────────────────────────────────────────────────
async def _job_counts(db: AsyncSession, ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {i: {} for i in ids}
    if ids:
        for jid, st, n in await db.execute(
                select(OtaItem.job_id, OtaItem.status, func.count()).where(OtaItem.job_id.in_(ids))
                .group_by(OtaItem.job_id, OtaItem.status)):
            out[jid][st] = int(n)
    return out


def _job_out(j: OtaJob, c: dict[str, int]) -> dict[str, Any]:
    return {"id": j.id, "package_id": j.package_id, "filename": j.filename, "version": j.version,
            "size": j.size, "sha256": j.sha256, "scope_kind": j.scope_kind, "scope_label": j.scope_label,
            "total": j.total, "created_by": j.created_by, "created_at": j.created_at,
            "finished_at": j.finished_at, "cancelled_at": j.cancelled_at, "counts": c}


@router.get("/jobs")
async def list_jobs(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    total = await db.scalar(select(func.count()).select_from(OtaJob)) or 0
    jobs = (await db.execute(select(OtaJob).order_by(OtaJob.id.desc())
                             .offset((page - 1) * size).limit(size))).scalars().all()
    counts = await _job_counts(db, [j.id for j in jobs])
    return {"items": [_job_out(j, counts[j.id]) for j in jobs], "total": int(total), "page": page, "size": size}


@router.get("/jobs/{jid}")
async def get_job(jid: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    j = await db.get(OtaJob, jid)
    if j is None:
        raise NotFound("없는 OTA 작업입니다.", code="OTA_JOB_NOT_FOUND")
    now = _now()
    rows = (await db.execute(
        select(OtaItem, Device.site, Device.fw, presence.online_clause(now).label("online_now"))
        .join(Device, Device.uuid == OtaItem.uuid, isouter=True)
        .where(OtaItem.job_id == jid).order_by(OtaItem.id))).all()
    items = [{"id": it.id, "uuid": it.uuid, "site": site, "status": it.status, "detail": it.detail,
              "attempts": it.attempts, "downloads": it.downloads, "sent_at": it.first_sent_at,
              "started_at": it.started_at, "finished_at": it.finished_at, "fw_before": it.fw_before,
              "fw_after": it.fw_after, "fw_now": fw, "online": bool(on)} for it, site, fw, on in rows]
    counts = await _job_counts(db, [jid])
    return {**_job_out(j, counts[jid]), "items": items}


@router.post("/jobs/{jid}/cancel")
async def cancel_job(jid: int, me: Principal = Depends(current_user),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    j = await db.get(OtaJob, jid, with_for_update=True)
    if j is None:
        raise NotFound("없는 OTA 작업입니다.", code="OTA_JOB_NOT_FOUND")
    now = _now()
    items = (await db.execute(select(OtaItem).where(OtaItem.job_id == jid, OtaItem.status == "waiting")
                              .with_for_update())).scalars().all()
    for it in items:
        it.status, it.detail, it.finished_at, it.updated_at = "CANCELLED", f"취소({me.user})", now, now
    if j.cancelled_at is None:
        j.cancelled_at, j.cancelled_by = now, me.user
    from app.mqtt.ota_runner import close_finished_jobs  # noqa: PLC0415
    await close_finished_jobs(db, [jid], now)
    log.info("OTA 작업 #%d 취소 — 대기 %d대 by %s", jid, len(items), me.user)
    return {"cancelled": len(items)}


@router.post("/jobs/{jid}/retry", status_code=status.HTTP_201_CREATED)
async def retry_job(jid: int, request: Request, me: Principal = Depends(current_user),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    j = await db.get(OtaJob, jid)
    if j is None:
        raise NotFound("없는 OTA 작업입니다.", code="OTA_JOB_NOT_FOUND")
    pkg = await db.get(OtaPackage, j.package_id) if j.package_id else None
    if pkg is None:
        raise Conflict("이 작업의 펌웨어 파일이 지워졌습니다 — 파일을 다시 올려 새로 보내세요.", code="OTA_PACKAGE_GONE")
    uuids = [r[0] for r in await db.execute(
        select(OtaItem.uuid).join(Device, Device.uuid == OtaItem.uuid)
        .where(OtaItem.job_id == jid, OtaItem.status.in_(RETRY_STATUSES),
               Device.state == DeviceState.ACTIVE.value).order_by(OtaItem.id))]
    out = await _create_job(db, pkg, kind="retry", scope_id=str(jid),
                            label=f"#{jid} 실패·응답 없음 다시 ({j.scope_label or ''})", uuids=uuids, me=me)
    if (r := _runner(request)) is not None:
        r.wake()
    return out
