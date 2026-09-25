"""단말 온라인 판정 — 한 곳에서만 정의한다.

판정이 두 벌이면 화면마다 다른 말을 한다(목록은 온라인인데 상세는 오프라인).
파이썬 판(is_online)과 SQL 판(online_clause)이 **반드시 같은 규칙**이어야 하므로
둘을 나란히 둔다.

규칙 (사양서 §16.1, docs/00 §4):
    online  =  device.online 플래그 (LWT/CONNECT 기반, 3차)
            OR (2차 대체) 마지막 Telemetry 가 `ti × DEVICE_ONLINE_FACTOR` 안
                          그리고 그 뒤로 LWT 를 받지 않았음

`ti` 는 단말이 보고한 ti_device 를 우선하고 없으면 ti_server 를 쓴다 — 서버가 300 으로
바꿨는데 단말이 아직 600 으로 보내고 있으면 600 기준으로 봐야 오프라인 오판이 없다.

"그 뒤로 LWT 를 받지 않았음" 조건이 없으면 LWT 직후에도 마지막 Telemetry 가 최근이라
온라인으로 남는다. 브로커가 사망을 통지했는데 화면이 살아 있다고 하면 안 된다.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from sqlalchemy import and_, func, or_, text

from app.config import settings
from app.models.device import Device


class _PresenceFields(Protocol):
    online: bool
    last_telemetry_at: dt.datetime | None
    offline_at: dt.datetime | None
    ti_device: int | None
    ti_server: int


def online_window_sec(ti_device: int | None, ti_server: int, factor: int | None = None) -> int:
    factor = settings.device_online_factor if factor is None else factor
    return int((ti_device or ti_server) * factor)


def is_online(device: _PresenceFields, now: dt.datetime, factor: int | None = None) -> bool:
    if device.online:
        return True
    last = device.last_telemetry_at
    if last is None:
        return False
    if device.offline_at is not None and device.offline_at >= last:
        return False
    window = online_window_sec(device.ti_device, device.ti_server, factor)
    return last >= now - dt.timedelta(seconds=window)


def online_clause(now: dt.datetime):
    """is_online() 의 SQL 판. 규칙이 바뀌면 둘을 같이 고친다."""
    window = func.coalesce(Device.ti_device, Device.ti_server) * settings.device_online_factor
    cutoff = now - window * text("interval '1 second'")
    return or_(
        Device.online.is_(True),
        and_(
            Device.last_telemetry_at.is_not(None),
            Device.last_telemetry_at >= cutoff,
            or_(Device.offline_at.is_(None), Device.offline_at < Device.last_telemetry_at),
        ),
    )
