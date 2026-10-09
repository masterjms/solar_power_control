"""단말 기록 내보내기(문제점 48번) — 한 단말의 알람·명령 이력·보고·이벤트·설정 변경을 엑셀 파일 하나로.

CSV 는 표 하나만 담을 수 있어 기록 종류(5가지)마다 파일이 따로 생긴다. 분석은 엑셀에서 하므로
.xlsx 한 파일에 시트를 나눈다(시트마다 열 이름은 한국어, 시각은 한국 시각, 값은 화면과 같은 단위).
원래 값을 그대로 보고 싶을 때를 위해 보고·이벤트·알람에는 원문(JSON) 열을 남긴다.

범위: 최근 days 일(1~90). 보고는 최대 MAX_TELEMETRY 줄.
권한: 경로에 {uuid} 가 있어 공통 접근 검사가 범위 밖 단말을 404 로 막고, 게스트는 GET 허용 목록에 없어 403.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MsgType
from app.core import alarm_rules as ar
from app.core import settings_rules as sr
from app.models.alarm import Alarm
from app.models.command import Command, CommandTarget
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.settings import DeviceSettingsHistory
from app.models.telemetry import Telemetry
from app.modules.activity.router import STATE_LABEL, _cmd_text
from app.modules.region.service import load_tree

KST = ZoneInfo("Asia/Seoul")
MAX_TELEMETRY = 200_000

ACK_LABEL = {"OK": "응답", "LOCAL": "현장 조작 중", "EXPIRED": "만료", "NO_RESPONSE": "무응답",
             "OFFLINE": "오프라인 — 안 보냄", "pending": "응답 대기", "BAD": "오류", "STATE": "상태 오류"}
EVENT_LABEL = {
    "REGISTER": "처음 접속", "REGISTER_ACK": "승인 정보", "STATE_CHANGE": "상태 변경", "CONFIG_SET": "설정 보내기",
    "CONFIG_ACK": "단말 응답", "PING": "연결 확인", "PONG": "응답", "ONLINE": "접속", "OFFLINE": "연결 끊김",
    "LWT": "연결 끊김", "LOST": "보고 빠짐", "REBOOT": "재부팅", "ERR": "오류", "CMD_ACK": "명령 응답",
    "COMMAND_SENT": "원격 명령 보냄", "COMMAND_ACK": "명령 응답", "SETTINGS_SENT": "설정 보내기",
    "SETTINGS": "단말 설정 읽음", "SETTINGS_ACK": "설정 응답",
}
MD_LABEL = {0: "스케줄", 1: "현장 수동", 2: "원격 조작"}


def _t(v: dt.datetime | None) -> dt.datetime | None:
    """엑셀 칸 — 한국 시각, 시간대 표시 없이(엑셀이 날짜로 정렬·필터할 수 있게)."""
    return v.astimezone(KST).replace(tzinfo=None, microsecond=0) if v else None


def _x100(v: int | None) -> float | None:
    return None if v is None else round(v / 100, 2)


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def _scaled(it: dict[str, Any] | None, v: int | None) -> float | int | None:
    """설정 값 — 화면과 같은 단위(배율 100 이면 소수 2자리)."""
    if v is None or not it or int(it.get("scale") or 1) <= 1:
        return v
    return round(v / int(it["scale"]), 2)


def _by(v: str | None) -> str | None:
    """누가 — 서버 화면 사용자 이름, 현장 변경은 '현장'."""
    return {"local": "현장", "device_read": "단말에서 읽음", "server_write": "서버가 씀"}.get(v or "", v)


def _sheet(wb: Workbook, title: str, head: list[str], rows: list[list[Any]]) -> None:
    ws = wb.create_sheet(title)
    bold = Font(bold=True)
    cells = []
    for h in head:
        c = WriteOnlyCell(ws, value=h)
        c.font = bold
        cells.append(c)
    ws.append(cells)
    for r in rows:
        ws.append(r)


async def build(db: AsyncSession, uuid: str, days: int, by: str) -> tuple[bytes, str]:
    """(xlsx 바이트, 파일 이름)."""
    dev = await db.get(Device, uuid)
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    tree = await load_tree(db)
    region = tree.path_name(dev.node_id) if dev and dev.node_id else None

    wb = Workbook(write_only=True)

    # ── 단말 정보 ──
    info = [
        ["UUID", uuid], ["시설명", dev.site if dev else None], ["지역", region],
        ["주소", getattr(dev, "address", None)],
        ["상태", STATE_LABEL.get(dev.state, dev.state) if dev else None],
        ["처음 접속", _t(getattr(dev, "created_at", None))], ["마지막 수신", _t(getattr(dev, "last_seen_at", None))],
        ["펌웨어", getattr(dev, "fw", None)], ["모델", getattr(dev, "device_model", None)],
        ["IMEI", getattr(dev, "imei", None)],
        ["기간", f"최근 {days}일 ({_t(since):%Y-%m-%d %H:%M} ~ {_t(now):%Y-%m-%d %H:%M}, 한국 시각)"],
        ["내보낸 사람", by], ["내보낸 시각", _t(now)],
    ]
    _sheet(wb, "단말 정보", ["항목", "값"], info)

    # ── 알람 ──
    rows = []
    for a in (await db.execute(
        select(Alarm).where(Alarm.uuid == uuid,
                            or_(Alarm.last_seen_at >= since, Alarm.closed_at.is_(None)))
        .order_by(Alarm.first_seen_at.desc()))).scalars():
        rows.append([_t(a.opened_at), _t(a.closed_at),
                     ar.KINDS[a.kind].label if a.kind in ar.KINDS else a.kind, a.severity,
                     _t(a.first_seen_at), _t(a.last_seen_at), "해제" if a.closed_at else "진행 중", _j(a.value)])
    _sheet(wb, "알람", ["발생", "해제", "항목", "등급", "처음 감지", "마지막 감지", "지금", "값(원문)"], rows)

    # ── 명령 이력(이 단말이 대상에 든 원격 명령) ──
    rows = []
    q = (select(Command, CommandTarget)
         .join(CommandTarget, CommandTarget.seq == Command.seq)
         .where(CommandTarget.uuid == uuid, Command.type == MsgType.COMMAND.value, Command.sent_at >= since)
         .order_by(Command.sent_at.desc()))
    for c, t in (await db.execute(q)).all():
        target = {"device": "이 단말", "node": "지역 전체", "all": "전체 단말"}.get(c.target_kind, c.target_kind)
        rows.append([_t(c.sent_at), c.created_by, target, _cmd_text(c).split(" — ")[0],
                     ACK_LABEL.get(t.status, t.status), t.attempts, _t(t.last_sent_at), _t(t.acked_at),
                     c.seq, _j(c.payload), _j(t.ack)])
    _sheet(wb, "명령 이력", ["보낸 시각", "누가", "대상", "명령", "이 단말 결과", "보낸 횟수", "마지막 발송",
                         "응답 시각", "명령 번호", "보낸 원문", "응답 원문"], rows)

    # ── 보고(텔레메트리) ──
    rows = []
    q = (select(Telemetry).where(Telemetry.uuid == uuid, Telemetry.received_at >= since)
         .order_by(Telemetry.received_at.desc()).limit(MAX_TELEMETRY))
    for r in (await db.execute(q)).scalars():
        raw = r.raw or {}
        rows.append([
            _t(r.received_at), r.ts_device, r.sq, r.fw,
            {1: "켜짐", 0: "꺼짐"}.get(r.on_, r.on_), MD_LABEL.get(r.md, r.md), r.pw1, r.pw2, r.pw3,
            _x100(r.bv), _x100(r.bi), r.sc, _x100(r.pp), _x100(r.li),
            _x100(raw.get("eg")), _x100(raw.get("eu")), _x100(raw.get("yg")), _x100(raw.get("yu")),
            None if r.er is None else f"0x{r.er:04X}",
            ", ".join(ar.KINDS[k].label for k in ar.er_kinds(r.er or 0)) or None,
            r.cv, r.ss, _j(raw),
        ])
    _sheet(wb, "보고", ["받은 시각", "단말 시각", "순번", "펌웨어", "조명", "운전 모드", "밝기1(%)", "밝기2(%)", "밝기3(%)",
                      "배터리(V)", "배터리 전류(A)", "잔량(%)", "패널 출력(W)", "부하 전류(A)",
                      "오늘 발전(kWh)", "오늘 사용(kWh)", "어제 발전(kWh)", "어제 사용(kWh)",
                      "오류 코드", "오류 내용", "설정 번호", "저장 번호", "원문"], rows)

    # ── 이벤트 ──
    rows = []
    for e in (await db.execute(
        select(DeviceEvent).where(DeviceEvent.uuid == uuid, DeviceEvent.received_at >= since)
        .order_by(DeviceEvent.received_at.desc()))).scalars():
        rows.append([_t(e.received_at), EVENT_LABEL.get(e.kind, e.kind), e.kind, _j(e.payload)])
    _sheet(wb, "이벤트", ["시각", "종류", "종류 코드", "내용(원문)"], rows)

    # ── 설정 변경 이력 ── 항목 이름·단위·배율은 화면과 같은 ui_items.json
    items = {i["key"]: i for i in sr.items()}
    rows = []
    for h in (await db.execute(
        select(DeviceSettingsHistory).where(DeviceSettingsHistory.uuid == uuid,
                                            DeviceSettingsHistory.changed_at >= since)
        .order_by(DeviceSettingsHistory.changed_at.desc()))).scalars():
        it = items.get(h.key)
        label = "스케줄 표" if h.key == "tbl" else (f"{it['label']}({it['unit']})" if it and it.get("unit") else
                                                   it["label"] if it else h.key)
        rows.append([_t(h.changed_at), _by(h.by), label, _scaled(it, h.old), _scaled(it, h.new), h.note, h.key])
    _sheet(wb, "설정 변경", ["시각", "누가", "항목", "이전", "새 값", "비고", "항목 코드"], rows)

    buf = io.BytesIO()
    wb.save(buf)
    name = f"단말기록_{(dev.site if dev and dev.site else uuid[-6:])}_{_t(now):%Y%m%d}_{days}일.xlsx"
    return buf.getvalue(), name
