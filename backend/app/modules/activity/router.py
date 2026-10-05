"""/api/activity — 대시보드 "최근 활동"(문제점 30·34번). 새 표 없이 기존 기록을 합쳐 최신순으로 보여 준다.

넣는 것(분류)
  alarm    알람 발생·해제(alarm.opened_at / closed_at)
  device   상태 바뀜(승인·중지·복귀·거절·폐기·재등록), 재부팅(device_event STATE_CHANGE / REBOOT)
  control  원격 명령(명령 1건에 1줄 — 단말마다가 아님), 그룹 스케줄 보내기 시작
  admin    로그인 실패, 서버 설정 변경, 계정 만듦 — 최고관리자만
빼는 것: 10분 보고, 내부 응답(CONFIG/SETTINGS), 재발송, 브로커의 순간 끊김·재접속(통신 두절은 알람으로만 — 1만 대에서
화면이 그것으로만 차지 않게), 로그인 성공.

지역관리자는 맡은 시·도 단말 것만(명령·보내기는 대상에 맡은 지역 단말이 하나라도 있으면), 관리 분류는 없다.
검색 q: 시설명·주소·지역(동·시군구·시도 이름) 부분 일치 — 단말에 묶인 줄만 찾는다(명령·보내기는 대상 이름으로).

여러 표를 합쳐 쪽을 나누므로, 원천마다 (쪽 끝까지) 최신순으로 읽어 합친 뒤 자른다. 전체 건수는 원천별 건수의 합.
쪽 넘기기는 최근 MAX_ROWS 건까지(쪽×개수) — 깊은 쪽은 원천마다 그만큼 읽어야 해서. 그 전 기록은 검색·분류로 좁힌다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.constants import EventKind, MsgType
from app.core import alarm_rules as ar
from app.core.access import current_scope, in_scope
from app.core.auth import Principal, current_user
from app.db import get_db
from app.models.alarm import Alarm
from app.models.command import Command, CommandTarget
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.region import Region
from app.models.schedule import DeployItem, DeployJob
from app.models.system import AdminUser, LoginLog, ServerSetting
from app.modules.region.service import load_tree

router = APIRouter(tags=["activity"])

CATS = ("alarm", "device", "control", "admin")
MAX_ROWS = 2000
STATE_LABEL = {"PENDING": "승인 대기", "ACTIVE": "운영", "SUSPENDED": "일시 중지", "REJECTED": "거절",
               "RETIRED": "폐기"}
ACT_LABEL = {"off": "소등", "on": "점등", "pwm": "밝기", "auto": "스케줄 복귀"}
RESULT_LABEL = {"OK": "전부 성공", "PARTIAL": "일부 성공", "TIMEOUT": "응답 없음"}
CH_LABEL = {1: "주등", 2: "입간판", 3: "PWM3"}


def _esc(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _device_q(q: str | None):  # noqa: ANN202
    """단말 검색 조건(시설명·주소·지역 이름) — 단말 목록 검색과 같은 규칙."""
    if not q or not q.strip():
        return None
    needle = _esc(q.strip())
    leaf, mid, top = aliased(Region), aliased(Region), aliased(Region)
    region_hit = (
        select(leaf.id).join(mid, mid.id == leaf.parent_id, isouter=True)
        .join(top, top.id == mid.parent_id, isouter=True)
        .where(leaf.name.ilike(needle, escape="\\") | mid.name.ilike(needle, escape="\\")
               | top.name.ilike(needle, escape="\\"))
    )
    return or_(Device.site.ilike(needle, escape="\\"), Device.address.ilike(needle, escape="\\"),
               Device.uuid.ilike(needle, escape="\\"), Device.node_id.in_(region_hit))


async def _page(db: AsyncSession, stmt: Select, at_col, limit: int) -> tuple[list, int]:  # noqa: ANN001
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await db.execute(stmt.order_by(at_col.desc()).limit(limit))).all()
    return rows, total


def _cmd_text(c: Command) -> str:
    p = c.payload or {}
    act = ACT_LABEL.get(str(p.get("act")), str(p.get("act") or "명령"))
    chs = p.get("ch") or []
    ch = "·".join(CH_LABEL.get(int(x), str(x)) for x in chs) if isinstance(chs, list) else ""
    level = ""
    if p.get("act") == "pwm" and isinstance(p.get("pwm"), list):
        level = " " + "/".join(f"{v}%" for v in p["pwm"])
    res = RESULT_LABEL.get(c.result or "", "진행 중" if c.finished_at is None else (c.result or ""))
    return f"원격 {act}{level}{f' ({ch})' if ch else ''} — {res} {c.acked_count}/{c.expected_count}대"


@router.get("/api/activity")
async def activity(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    cat: str | None = Query(default=None, pattern="^(alarm|device|control|admin)$"),
    q: str | None = Query(default=None, max_length=64),
    me: Principal = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    want = page * size  # 원천마다 이만큼만 읽어 합친다
    if want > MAX_ROWS:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "page"), "input": page,
                                       "msg": f"최근 {MAX_ROWS}건까지만 쪽으로 넘길 수 있습니다 — 검색·분류로 좁히세요."}])
    tree = await load_tree(db)
    dq = _device_q(q)
    items: list[dict[str, Any]] = []
    total = 0
    cats = [cat] if cat else list(CATS)
    if not me.is_super and "admin" in cats:
        cats.remove("admin")

    def dev_row(at, cat_, kind, uuid, site, node_id, text, **extra) -> dict[str, Any]:  # noqa: ANN001
        return {"at": at, "cat": cat_, "kind": kind, "uuid": uuid, "site": site,
                "region": tree.path_name(node_id) if node_id else None, "text": text, **extra}

    # ── 알람 발생·해제 ──
    if "alarm" in cats:
        for col, kind in ((Alarm.opened_at, "ALARM_OPEN"), (Alarm.closed_at, "ALARM_CLOSE")):
            stmt = (select(col.label("at"), Alarm.kind, Alarm.severity, Alarm.uuid, Device.site,
                           Device.node_id)
                    .join(Device, Device.uuid == Alarm.uuid, isouter=True)
                    .where(col.is_not(None), Alarm.opened_at.is_not(None), in_scope(Device.node_id)))
            if dq is not None:
                stmt = stmt.where(dq)
            rows, n = await _page(db, stmt, col, want)
            total += n
            for at, k, sev, uuid, site, node in rows:
                label = ar.KINDS[k].label if k in ar.KINDS else k
                items.append(dev_row(at, "alarm", kind, uuid, site, node,
                                     f"{label} {'발생' if kind == 'ALARM_OPEN' else '해제'}",
                                     severity=sev if kind == "ALARM_OPEN" else "info"))

    # ── 단말 상태 바뀜·재부팅 ──
    if "device" in cats:
        stmt = (select(DeviceEvent.received_at, DeviceEvent.kind, DeviceEvent.payload, DeviceEvent.uuid,
                       Device.site, Device.node_id)
                .join(Device, Device.uuid == DeviceEvent.uuid, isouter=True)
                .where(DeviceEvent.kind.in_((EventKind.STATE_CHANGE.value, EventKind.REBOOT.value)),
                       in_scope(Device.node_id)))
        if dq is not None:
            stmt = stmt.where(dq)
        rows, n = await _page(db, stmt, DeviceEvent.received_at, want)
        total += n
        for at, k, payload, uuid, site, node in rows:
            p = payload or {}
            if k == EventKind.REBOOT.value:
                text = "재부팅(보고 번호가 처음부터)"
            else:
                frm, to = STATE_LABEL.get(p.get("from"), p.get("from")), STATE_LABEL.get(p.get("to"), p.get("to"))
                text = f"상태 {frm} → {to}" + (" (단말 재등록)" if p.get("by") == "register" else "")
            items.append(dev_row(at, "device", k, uuid, site or p.get("site"), node, text))

    # ── 원격 명령(1건 1줄)·그룹 스케줄 보내기 ──
    if "control" in cats:
        scope = current_scope()
        stmt = select(Command).where(Command.type == MsgType.COMMAND.value)
        if scope is not None:
            stmt = stmt.where(Command.seq.in_(
                select(CommandTarget.seq).join(Device, Device.uuid == CommandTarget.uuid)
                .where(in_scope(Device.node_id))))
        if dq is not None:
            stmt = stmt.where(Command.seq.in_(
                select(CommandTarget.seq).join(Device, Device.uuid == CommandTarget.uuid).where(dq)))
        total += int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        for c in (await db.execute(stmt.order_by(Command.sent_at.desc()).limit(want))).scalars():
            site = region = uuid = None
            if c.target_kind == "device":
                d = await db.get(Device, c.target_id)
                uuid, site = c.target_id, d.site if d else None
                region = tree.path_name(d.node_id) if d and d.node_id else None
            elif c.target_kind == "node":
                region = tree.path_name(int(c.target_id)) if c.target_id and c.target_id.isdigit() else None
                site = "지역 전체"
            else:
                site = "전체 단말"
            items.append({"at": c.sent_at, "cat": "control", "kind": "COMMAND", "uuid": uuid, "site": site,
                          "region": region, "text": _cmd_text(c), "by": c.created_by, "seq": c.seq})
        stmt = select(DeployJob)
        if scope is not None or dq is not None:
            sub = select(DeployItem.job_id).join(Device, Device.uuid == DeployItem.uuid)
            if scope is not None:
                sub = sub.where(in_scope(Device.node_id))
            if dq is not None:
                sub = sub.where(dq)
            stmt = stmt.where(DeployJob.id.in_(sub))
        total += int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        for j in (await db.execute(stmt.order_by(DeployJob.created_at.desc()).limit(want))).scalars():
            state = "취소" if j.cancelled_at else "끝남" if j.finished_at else "진행 중"
            items.append({"at": j.created_at, "cat": "control", "kind": "DEPLOY", "uuid": None,
                          "site": "그룹 스케줄 변경", "region": j.scope_label,
                          "text": f"스케줄 양식 '{j.profile_name}' 보내기 {j.total}대 — {state}",
                          "by": j.created_by, "job_id": j.id})

    # ── 관리(최고관리자) ──
    if "admin" in cats and not (q and q.strip()):
        stmt = select(LoginLog).where(LoginLog.ok.is_(False))
        total += int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        reason = {"bad_password": "비밀번호 틀림", "expired": "기간 만료", "disabled": "사용 중지"}
        for r in (await db.execute(stmt.order_by(LoginLog.at.desc()).limit(want))).scalars():
            items.append({"at": r.at, "cat": "admin", "kind": "LOGIN_FAIL", "uuid": None, "site": None,
                          "region": None, "text": f"로그인 실패 {r.username} — {reason.get(r.reason, r.reason)}"
                          + (f" ({r.ip})" if r.ip else ""), "by": r.username})
        for s in (await db.execute(select(ServerSetting).order_by(ServerSetting.updated_at.desc())
                                   .limit(want))).scalars():
            total += 1
            items.append({"at": s.updated_at, "cat": "admin", "kind": "SETTING", "uuid": None, "site": None,
                          "region": None, "text": f"서버 설정 {s.key} = {s.value}", "by": s.updated_by})
        for a in (await db.execute(select(AdminUser).order_by(AdminUser.created_at.desc()).limit(want))).scalars():
            total += 1
            items.append({"at": a.created_at, "cat": "admin", "kind": "ACCOUNT", "uuid": None, "site": None,
                          "region": None, "text": f"계정 만듦 {a.username}", "by": a.created_by})

    epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    items.sort(key=lambda x: x["at"] or epoch, reverse=True)
    start = (page - 1) * size
    return {"items": items[start:start + size], "total": total, "page": page, "size": size, "max_rows": MAX_ROWS,
            "cats": [c for c in CATS if me.is_super or c != "admin"]}
