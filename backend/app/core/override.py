"""device.override_ch(채널별 원격) → 요약 override_* 4개를 같이 쓴다(§3.10.8, 마이그레이션 0005).

요약은 목록 필터(remote=true)와 "원격 n분 남음"이 쓰는 기존 컬럼이다. 둘이 어긋나지 않게
override_ch 를 바꾸는 곳은 전부 여기를 거친다(명령 OK, 재부팅·현장 조작으로 지움).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.command_rules import override_summary
from app.models.device import Device


def summary_values(channels: dict[str, Any] | None, now: dt.datetime) -> dict[str, Any]:
    s = override_summary(channels, now)
    return {
        "override_ch": channels or None,
        "override_act": s.act, "override_level": s.level,
        "override_seq": s.seq, "override_until": s.until,
    }


def apply_channels(device: Device, channels: dict[str, Any] | None, now: dt.datetime) -> None:
    for k, v in summary_values(channels, now).items():
        setattr(device, k, v)
