"""cv / 적용값 / 전송 판정 순수 규칙 (사양서 §1.1.7 S-13, §4.2)."""

from __future__ import annotations

import pytest

from app.core.config_rules import (
    bump_cv_server,
    effective_config,
    next_cv_server,
    should_send_config,
)


def test_effective_override_beats_profile():
    e = effective_config(ti_override=None, ka_override=None, profile_ti=600, profile_ka=300)
    assert (e.ti, e.ka) == (600, 300)
    e = effective_config(ti_override=120, ka_override=None, profile_ti=600, profile_ka=300)
    assert (e.ti, e.ka) == (120, 300)
    e = effective_config(ti_override=None, ka_override=900, profile_ti=600, profile_ka=300)
    assert (e.ti, e.ka) == (600, 900)


@pytest.mark.parametrize("cv_server,cv_device,expected", [
    (0, None, 1),      # 규칙 2: 0 은 보내지 않는다 → 1
    (0, 0, 1),
    (0, 5, 6),         # 규칙 3: 단말이 더 크면 +1
    (3, 0, 3),
    (3, 3, 3),
    (3, 7, 8),
    (65535, 65535, 65535),
    (65535, 65540, 1), # 넘치면 1 (0 은 건너뜀)
])
def test_next_cv_server(cv_server, cv_device, expected):
    assert next_cv_server(cv_server, cv_device) == expected
    assert next_cv_server(cv_server, cv_device) >= 1


@pytest.mark.parametrize("cv_server,cv_device,expected", [
    (0, None, 1),
    (0, 4, 5),
    (3, None, 4),
    (3, 9, 10),
    (65535, None, 1),
])
def test_bump_cv_server(cv_server, cv_device, expected):
    assert bump_cv_server(cv_server, cv_device) == expected


def test_should_send_only_active_and_mismatch():
    assert should_send_config("ACTIVE", 0, 0) is False     # 같다(둘 다 0) → 안 보냄
    assert should_send_config("ACTIVE", 1, 0) is True
    assert should_send_config("ACTIVE", 3, 3) is False
    assert should_send_config("ACTIVE", 3, 7) is True
    assert should_send_config("ACTIVE", 3, None) is False  # 1차 펌웨어, cv 모름
    for state in ("PENDING", "SUSPENDED", "REJECTED", "RETIRED"):
        assert should_send_config(state, 3, 0) is False
