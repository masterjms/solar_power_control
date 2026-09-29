from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel


class AlarmOut(BaseModel):
    id: int
    uuid: str
    kind: str
    #: 화면 이름(LED FAULT, 통신 두절 …)
    label: str
    #: fault / comm / pending / config / local
    tab: str | None
    #: warn(경고) / caution(주의) / info(정보)
    severity: str
    #: 조건이 처음 보인 시각 = 발생.
    first_seen_at: dt.datetime
    #: 연 시각(지속 조건 항목은 first_seen + 기준 시간).
    opened_at: dt.datetime | None
    last_seen_at: dt.datetime
    closed_at: dt.datetime | None
    #: 발생 ~ 해제(열림이면 지금).
    duration_sec: int
    value: dict[str, Any] | None
    site: str | None
    address: str | None
    node_path: str | None
    state: str | None


class AlarmPage(BaseModel):
    items: list[AlarmOut]
    total: int
    page: int
    size: int
    #: 열린 알람 탭별 건수 {all, fault, comm, pending, config, local}
    counts: dict[str, int]
