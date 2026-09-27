"""원격 명령 API 스키마 (docs/05 "5차 API — 명령")."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field


class TargetIn(BaseModel):
    kind: Literal["device", "node", "all"]
    #: device = uuid, node = region id(숫자 또는 문자열), all = null.
    id: str | int | None = None


class CommandIn(BaseModel):
    """공통 요청 본문. 값 규칙은 core/command_rules.validate_command (422 VALIDATION_FAILED)."""

    target: TargetIn
    act: str
    ch: list[int] | None = None
    pwm: list[int] | None = None
    dur: int | None = None
    dur_preset: str | None = None
    exp: int | None = None


class PreviewOut(BaseModel):
    #: 대상 ACTIVE 단말 수(스냅숏이 될 수).
    expected: int
    online: int
    offline: int
    #: 대상 중 last_telemetry.er & 1 (BATT_LOW) — 점등 명령이어도 안 켜질 수 있다.
    low_battery: int
    #: 범위 안이지만 ACTIVE 가 아니라 빠지는 수.
    not_active: int
    topics: list[str]
    #: 확정된 유지시간(초). auto 는 null. tonight 이면 계산값.
    dur: int | None
    #: 보낼 payload(seq 제외 — 보낼 때 발번).
    payload: dict[str, Any]


class TargetOut(BaseModel):
    kind: str
    id: str | None
    label: str


class CommandOut(BaseModel):
    seq: int
    target: TargetOut
    topics: list[str]
    payload: dict[str, Any]
    expected: int
    sent_at: dt.datetime
    created_by: str | None


class CommandItem(BaseModel):
    seq: int
    created_by: str | None
    target_kind: str
    target_id: str | None
    target_label: str
    act: str | None
    ch: list[int] | None
    pwm: list[int] | None
    dur: int | None
    sent_at: dt.datetime
    finished_at: dt.datetime | None
    #: OK / PARTIAL / TIMEOUT. 진행 중이면 null.
    result: str | None
    expected_count: int
    #: 대상 상태별 수. {"OK","LOCAL","EXPIRED","BAD","STATE","pending"} 전부 키가 있다.
    counts: dict[str, int]


class TargetRow(BaseModel):
    uuid: str
    site: str | None
    node_name: str | None
    status: str
    attempts: int
    last_sent_at: dt.datetime | None
    acked_at: dt.datetime | None
    is_online: bool


class CommandDetail(CommandItem):
    targets: list[TargetRow]


class RetryIn(BaseModel):
    #: null 이면 무응답·EXPIRED 대상 전부.
    uuids: list[str] | None = Field(default=None, max_length=20_000)


class RetryOut(BaseModel):
    resent: int
