"""알람 항목 정의와 재조정 판정(순수 함수) — ADR-009, 사양서 §16.6.

재조정 = "지금 열려 있어야 할 것(desired)" 과 "DB 의 열림·관찰 행(existing)" 을 맞춘다.
  · desired 에 있고 행이 없음   → 새 행. hold 0 이면 바로 열림, 아니면 관찰(opened_at NULL)
  · desired 에 있고 관찰 행     → hold 가 찼으면 열기, 아니면 last_seen 만
  · desired 에 있고 열린 행     → last_seen·value 갱신
  · desired 에 없고 열린 행     → 닫기(이력)
  · desired 에 없고 관찰 행     → 지우기(연 적이 없다 — 이력 아님)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

SEV_WARN = "warn"
SEV_CAUTION = "caution"
SEV_INFO = "info"
#: 정렬 순서(경고 → 주의 → 정보).
SEVERITY_RANK = {SEV_WARN: 0, SEV_CAUTION: 1, SEV_INFO: 2}

TAB_FAULT = "fault"
TAB_COMM = "comm"
TAB_PENDING = "pending"
TAB_CONFIG = "config"
TAB_LOCAL = "local"
TABS = (TAB_FAULT, TAB_COMM, TAB_PENDING, TAB_CONFIG, TAB_LOCAL)


@dataclass(frozen=True)
class Kind:
    tab: str
    severity: str
    label: str
    #: er bit(고장 항목만).
    er_bit: int | None = None


#: §16.6.2. 순서 = 화면 표 순서.
KINDS: dict[str, Kind] = {
    "LED_FAULT": Kind(TAB_FAULT, SEV_WARN, "LED FAULT", 0x0004),
    "BATT_LOW": Kind(TAB_FAULT, SEV_WARN, "배터리 저전압", 0x0001),
    "MPPT_OFFLINE": Kind(TAB_FAULT, SEV_WARN, "MPPT 무응답", 0x0010),
    "SCHEDULE_DEFAULT": Kind(TAB_FAULT, SEV_CAUTION, "기본 스케줄 사용", 0x0020),
    "RTC_INVALID": Kind(TAB_FAULT, SEV_CAUTION, "시각 미확보", 0x0002),
    "OFFLINE": Kind(TAB_COMM, SEV_WARN, "통신 두절"),
    "REBOOT_FREQUENT": Kind(TAB_COMM, SEV_CAUTION, "재부팅 잦음"),
    "PENDING": Kind(TAB_PENDING, SEV_INFO, "승인 대기"),
    "CONFIG_MISMATCH": Kind(TAB_CONFIG, SEV_CAUTION, "CONFIG 불일치"),
    "LOCAL_SAVED": Kind(TAB_CONFIG, SEV_INFO, "현장 설정 변경"),
    "SCHEDULE_MISMATCH": Kind(TAB_CONFIG, SEV_CAUTION, "스케줄 불일치"),
    "LOCAL_OPERATION": Kind(TAB_LOCAL, SEV_INFO, "현장 조작 중"),
}
ER_KINDS = {k: v.er_bit for k, v in KINDS.items() if v.er_bit is not None}


def er_kinds(er: int | None) -> list[str]:
    """er bit → 고장 kind 들. 0x0008(예약)·0x0040(LTE_OFFLINE) 은 알람이 아니다."""
    if not er:
        return []
    return [k for k, bit in ER_KINDS.items() if er & bit]


@dataclass(frozen=True)
class Want:
    """열려 있어야 할 알람 하나 — 값, 지속 조건(초, 0 = 바로), 조건이 실제로 시작된 시각(알면).

    since 가 있으면 새 행의 first_seen_at(= 화면의 "발생")으로 쓴다 — 통신 두절은 알람 기능이 처음 본 때가 아니라
    단말이 끊긴 때, 승인 대기는 처음 접속한 때가 발생이다."""

    value: dict[str, Any] | None = None
    hold_sec: int = 0
    since: dt.datetime | None = None


@dataclass(frozen=True)
class Row:
    id: int
    uuid: str
    kind: str
    first_seen_at: dt.datetime
    opened_at: dt.datetime | None


@dataclass
class Plan:
    #: (uuid, kind, value, since)
    insert_open: list[tuple[str, str, dict | None, dt.datetime | None]] = field(default_factory=list)
    insert_watch: list[tuple[str, str, dict | None, dt.datetime | None]] = field(default_factory=list)
    promote: list[tuple[int, dict | None]] = field(default_factory=list)
    touch: list[tuple[int, dict | None]] = field(default_factory=list)
    close: list[int] = field(default_factory=list)
    drop_watch: list[int] = field(default_factory=list)


def reconcile(
    desired: dict[tuple[str, str], Want], existing: list[Row], now: dt.datetime
) -> Plan:
    plan = Plan()
    seen: set[tuple[str, str]] = set()
    for row in existing:
        key = (row.uuid, row.kind)
        seen.add(key)
        want = desired.get(key)
        if want is None:
            (plan.close if row.opened_at is not None else plan.drop_watch).append(row.id)
            continue
        if row.opened_at is None:
            if (now - row.first_seen_at).total_seconds() >= want.hold_sec:
                plan.promote.append((row.id, want.value))
            else:
                plan.touch.append((row.id, want.value))
        else:
            plan.touch.append((row.id, want.value))
    for key, want in desired.items():
        if key in seen:
            continue
        uuid, kind = key
        (plan.insert_open if want.hold_sec <= 0 else plan.insert_watch).append(
            (uuid, kind, want.value, want.since))
    return plan


def severity_of(kind: str) -> str:
    return KINDS[kind].severity if kind in KINDS else SEV_INFO


def tab_of(kind: str) -> str | None:
    return KINDS[kind].tab if kind in KINDS else None
