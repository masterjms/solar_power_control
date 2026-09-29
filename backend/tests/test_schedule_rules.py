"""ADR-010 스케줄 배포 규칙(순수 함수)."""

from __future__ import annotations

import pytest

from app.core import schedule_rules as sr
from app.core import settings_rules as rules


def test_profile_keys_are_the_15_of_spec():
    keys = sr.profile_keys()
    assert len(keys) == 15
    assert keys[:3] == ("start_ofst", "stop_ofst", "start_pwm")
    # 단말별 10개는 프로필에 없다(§13.1 ①)
    for k in ("manual_40w", "manual_5w1", "manual_5w2", "fade", "cut12", "rtn12", "cut24",
              "rtn24", "cut_time", "rtn_time"):
        assert k not in keys
    assert set(keys) | {"manual_40w", "manual_5w1", "manual_5w2", "fade", "cut12", "rtn12",
                        "cut24", "rtn24", "cut_time", "rtn_time"} == set(rules.item_keys())


def test_validate_profile_values():
    d = sr.profile_defaults()
    assert sr.validate_profile_values(d) == d
    with pytest.raises(rules.SettingsInvalid) as e:
        sr.validate_profile_values({**d, "manual_40w": 50})
    assert e.value.code == "SETTINGS_INCOMPLETE"
    with pytest.raises(rules.SettingsInvalid) as e:
        sr.validate_profile_values({k: v for k, v in d.items() if k != "stage1_h"})
    assert e.value.code == "SETTINGS_INCOMPLETE"
    with pytest.raises(rules.SettingsInvalid) as e:
        sr.validate_profile_values({**d, "start_pwm": 101})
    assert e.value.code == "SETTINGS_RANGE"
    # 1→4단계가 24시간을 넘으면 RULE
    bad = {**d, "stage1_h": 20, "stage2_h": 23, "stage3_h": 10, "stage4_h": 21}  # 20→23→10→21 = 25시간
    with pytest.raises(rules.SettingsInvalid) as e:
        sr.validate_profile_values(bad)
    assert e.value.code == "SETTINGS_RULE"


def test_overlay_keeps_device_specific_values():
    """배포 = 단말 마지막 읽은 25개 + 프로필 15개. 현장 기준 밝기·배터리 값은 그대로(§13.1 ③)."""
    device = {**rules.defaults(), "manual_40w": 55, "cut24": 2400, "rtn24": 2700, "fade": 3,
              "stage1_pwm": 99}
    prof = {**sr.profile_defaults(), "stage1_pwm": 50, "start_ofst": -10}
    out = sr.overlay(device, prof)
    assert out["manual_40w"] == 55 and out["cut24"] == 2400 and out["fade"] == 3
    assert out["stage1_pwm"] == 50 and out["start_ofst"] == -10
    assert list(out) == list(rules.item_keys())
    # 단말 값 쪽 규칙 위반(차단 ≥ 복귀)은 배포에서도 막힌다
    with pytest.raises(rules.SettingsInvalid):
        sr.overlay({**device, "cut24": 2800, "rtn24": 2700}, prof)


def test_effective_profile_device_then_nearest_node():
    nodes = {1: 10, 2: 20}  # 1 시도 → 10, 2 시군구 → 20
    assert sr.effective_profile(99, [3, 2, 1], nodes) == (99, "device")
    assert sr.effective_profile(None, [3, 2, 1], nodes) == (20, "node:2")   # 가장 가까운 조상
    assert sr.effective_profile(None, [3, 5, 1], nodes) == (10, "node:1")
    assert sr.effective_profile(None, [], nodes) == (None, None)


def test_region_is_cleaned_before_check():
    t = rules.validate_table("﻿ 서울 ", 37.5665, 126.978, 0, 0)
    assert t.region == "서울" and t.crc == "69C1DF86"


def test_next_step():
    base = dict(device_state="ACTIVE", is_online=True, busy=False, request_result=None,
                sync="synced", values=rules.defaults())
    assert sr.next_step(status="waiting", **base)[0] == sr.STEP_WRITE
    assert sr.next_step(status="waiting", **{**base, "sync": "unknown"})[0] == sr.STEP_READ
    assert sr.next_step(status="waiting", **{**base, "sync": "local_saved"})[0] == sr.STEP_READ
    assert sr.next_step(status="waiting", **{**base, "is_online": False})[0] == sr.STEP_WAIT
    assert sr.next_step(status="waiting", **{**base, "busy": True})[0] == sr.STEP_WAIT
    assert sr.next_step(status="waiting", **{**base, "device_state": "SUSPENDED"})[:2] == \
        (sr.STEP_FINISH, "STATE")
    # 읽는 중
    assert sr.next_step(status="reading", **base)[0] == sr.STEP_WAIT
    assert sr.next_step(status="reading", **{**base, "request_result": "OK"})[0] == sr.STEP_WRITE
    assert sr.next_step(status="reading", **{**base, "request_result": "TIMEOUT"})[:2] == \
        (sr.STEP_FINISH, "NO_RESPONSE")
    # 보낸 뒤 결과
    for res, st in (("OK", "OK"), ("CRC", "CRC"), ("TIMEOUT", "NO_RESPONSE"), ("FLASH", "FLASH")):
        assert sr.next_step(status="sent", **{**base, "request_result": res})[:2] == \
            (sr.STEP_FINISH, st)
