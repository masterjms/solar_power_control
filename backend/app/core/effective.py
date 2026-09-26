"""적용값 SQL 조각 — `coalesce(override, profile.value)` (docs/03 device.ti_override).

presence(온라인 창), CONFIG_ACK OK(ti_device/ka_device 반영), 목록 조회가 같이 쓴다.
파이썬 판은 core/config_rules.effective_config().
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.device import Device
from app.models.profile import ConfigProfile


def effective_ti_sql():
    profile_ti = (
        select(ConfigProfile.ti).where(ConfigProfile.id == Device.profile_id).scalar_subquery()
    )
    return func.coalesce(Device.ti_override, profile_ti)


def effective_ka_sql():
    profile_ka = (
        select(ConfigProfile.ka).where(ConfigProfile.id == Device.profile_id).scalar_subquery()
    )
    return func.coalesce(Device.ka_override, profile_ka)
