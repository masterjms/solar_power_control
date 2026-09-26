"""적용값 SQL 조각 — `coalesce(override, profile.value)` (docs/03 device.ti_override).

presence(온라인 창), CONFIG_ACK OK(ti_device/ka_device 반영), 목록 조회가 같이 쓴다.
파이썬 판은 core/config_rules.effective_config().
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.models.device import Device
from app.models.profile import ConfigProfile


def _profile_value(column_name: str):
    # 바깥 쿼리가 ConfigProfile 을 조인하고 있으면(목록의 profile_name) 자동 상관이 ConfigProfile
    # 까지 바깥 것으로 묶어 "FROM 절이 없다"는 오류가 난다(S2-13 에서 실제 발생). 별칭을 쓰고
    # Device 만 명시적으로 상관시킨다.
    p = aliased(ConfigProfile)
    return (
        select(getattr(p, column_name))
        .where(p.id == Device.profile_id)
        .correlate(Device)
        .scalar_subquery()
    )


def effective_ti_sql():
    return func.coalesce(Device.ti_override, _profile_value("ti"))


def effective_ka_sql():
    return func.coalesce(Device.ka_override, _profile_value("ka"))
