"""PING/CONFIG_SET(OK·RANGE·FLASH, ka)/CMD 처리 — 사양서 §1.1.5, §1.1.7, §3.10.7."""

from __future__ import annotations

import pytest

from tools.sim.device import SimDevice, uuid_from_index, validate_cmd, validate_config_set


def make(**kw) -> SimDevice:
    """게이트를 끈 2cha 단말 — 핸들러 자체만 본다(게이트는 test_approval_gate)."""
    kw.setdefault("approval_gate", False)
    return SimDevice(uuid_from_index(1, 0x0202), password="pw", **kw)


def test_ping_pong_same_seq_and_uuid():
    d = make()
    assert d.handle_ping({"type": "PING", "seq": 41}) == {"type": "PONG", "seq": 41, "uuid": d.uuid}
    assert d.stats.ping_rx == 1


# ── CONFIG_SET ───────────────────────────────────────────────────────────

def test_config_set_ok_applies_and_acks():
    d = make(cv=3, ti=600, ka=300)
    ack = d.handle_config_set({"type": "CONFIG_SET", "cv": 4, "ti": 300, "ka": 600, "lat": 37.3617, "lon": 126.9352})
    assert ack == {"type": "CONFIG_ACK", "uuid": d.uuid, "cv": 4, "result": "OK"}
    assert d.cv == 4 and d.ti == 300 and d.ka == 600 and d.lat == 37.3617 and d.lon == 126.9352
    assert d.build_tm()["cv"] == 4  # Telemetry 로 echo
    assert d.stats.config_ack_ok == 1
    assert d.last_config_set["ka"] == 600


def test_ka_applies_at_next_connect_not_now():
    d = make(ka=300)
    d.ka_connected = 300  # 접속 중인 세션의 값
    d.handle_config_set({"cv": 1, "ti": 600, "ka": 900})
    assert d.ka == 900 and d.ka_connected == 300  # Flash 에는 저장, 세션은 그대로(§1.1.7)


@pytest.mark.parametrize("bad", [
    {"cv": 5, "ti": 10}, {"cv": 5, "ti": 59}, {"cv": 5, "ti": 3601}, {"cv": 5, "ti": "300"}, {"cv": 5, "ti": True},
    {"cv": 5, "ka": 59}, {"cv": 5, "ka": 1801}, {"cv": 5, "ka": "300"}, {"cv": 5, "ka": 300.0},
    {"cv": 70000, "ti": 600}, {"cv": -1}, {"ti": 600},  # cv 없음
    {"cv": 5, "lat": 91}, {"cv": 5, "lon": "x"}, {"cv": 5, "lat": True},
    {"cv": 5, "grp": ["1", "2", "3"]}, {"cv": 5, "grp": ["abc"]}, {"cv": 5, "grp": "4141011000"},
])
def test_config_set_range_keeps_old_values(bad):
    d = make(cv=3, ti=600, ka=300)
    ack = d.handle_config_set({"type": "CONFIG_SET", **bad})
    assert ack == {"type": "CONFIG_ACK", "uuid": d.uuid, "cv": 3, "result": "RANGE"}
    assert d.cv == 3 and d.ti == 600 and d.ka == 300 and d.lat is None and d.grp == []
    assert d.stats.config_ack_range == 1


def test_config_set_boundaries_ok():
    d = make()
    assert d.handle_config_set({"cv": 0, "ti": 60, "ka": 60})["result"] == "OK"
    assert d.handle_config_set({"cv": 65535, "ti": 3600, "ka": 1800})["result"] == "OK"
    assert d.handle_config_set({"cv": 7})["result"] == "OK" and d.ti == 3600 and d.ka == 1800  # 생략 = 유지(단말 규격)
    assert validate_config_set({"cv": 1, "grp": ["4141011000", "414101100001"]}) is None
    assert validate_config_set({"cv": 1, "ti": 600, "ka": 300}) is None


def test_config_set_ignored_n_times_then_accepted():
    d = make(ignore_config_set=2)
    assert d.handle_config_set({"cv": 1, "ti": 300, "ka": 300}) is None
    assert d.handle_config_set({"cv": 1, "ti": 300, "ka": 300}) is None
    assert d.cv == 0 and d.stats.config_set_ignored == 2
    assert d.handle_config_set({"cv": 1, "ti": 300, "ka": 300})["result"] == "OK"
    assert d.stats.config_set_rx == 3


def test_flash_fail_next_replies_flash_and_keeps_values():
    d = make(cv=2, ti=600, ka=300, flash_fail_next=2)
    for _ in range(2):
        ack = d.handle_config_set({"cv": 3, "ti": 300, "ka": 600})
        assert ack == {"type": "CONFIG_ACK", "uuid": d.uuid, "cv": 2, "result": "FLASH"}
        assert d.cv == 2 and d.ti == 600 and d.ka == 300
    assert d.stats.config_ack_flash == 2
    ack = d.handle_config_set({"cv": 3, "ti": 300, "ka": 600})  # 세 번째는 성공
    assert ack["result"] == "OK" and d.cv == 3 and d.ti == 300 and d.ka == 600


def test_ti_min_ka_min_lower_the_range_for_tests_only():
    d = make(ti_min=1, ka_min=1)
    assert d.handle_config_set({"cv": 1, "ti": 5, "ka": 10})["result"] == "OK" and d.ti == 5 and d.ka == 10
    assert d.handle_config_set({"cv": 2, "ti": 0, "ka": 10})["result"] == "RANGE"
    assert d.handle_config_set({"cv": 2, "ti": 5, "ka": 0})["result"] == "RANGE"
    assert make().handle_config_set({"cv": 1, "ti": 5, "ka": 300})["result"] == "RANGE"  # 기본은 사양 하한 60
    assert validate_config_set({"cv": 1, "ti": 5, "ka": 5}, ti_min=1, ka_min=1) is None


def test_flash_does_not_mask_range():
    d = make(flash_fail_next=1)
    assert d.handle_config_set({"cv": 1, "ti": 10, "ka": 300})["result"] == "RANGE"
    assert d.flash_fail_next == 1  # RANGE 가 먼저라 FLASH 플래그는 소비되지 않는다


# ── CMD ──────────────────────────────────────────────────────────────────

def test_cmd_ok_applies_override_and_acks():
    d = make()
    ack = d.handle_cmd({"type": "CMD", "seq": 12, "exp": 30, "act": "off", "dur": 3600})
    assert ack == {"type": "CMD_ACK", "uuid": d.uuid, "seq": 12, "result": "OK", "act": "off", "dur": 3600}
    tm = d.build_tm()
    assert tm["md"] == 2 and tm["on"] == 0 and tm["pw"] == [0, 0, 0]


def test_cmd_pwm_and_auto():
    d = make()
    ack = d.handle_cmd({"type": "CMD", "seq": 1, "exp": 30, "act": "pwm", "pwm": 40, "dur": 60})
    assert ack["pwm"] == 40
    tm = d.build_tm()
    assert tm["md"] == 2 and tm["on"] == 1 and tm["pw"] == [40, 40, 40]
    assert d.handle_cmd({"type": "CMD", "seq": 2, "exp": 30, "act": "auto", "dur": 1})["result"] == "OK"
    assert d.current_override() is None and d.build_tm()["md"] == 0


@pytest.mark.parametrize("bad", [
    {"seq": 1, "exp": 30, "act": "off"},                 # dur 누락
    {"seq": 1, "exp": 30, "act": "off", "dur": 0},       # dur 0
    {"seq": 1, "exp": 30, "act": "off", "dur": 86401},   # 24시간 초과
    {"seq": 1, "act": "off", "dur": 10},                 # exp 누락
    {"seq": 1, "exp": 0, "act": "off", "dur": 10},
    {"exp": 30, "act": "off", "dur": 10},                # seq 누락
    {"seq": 1, "exp": 30, "act": "blink", "dur": 10},
    {"seq": 1, "exp": 30, "act": "pwm", "dur": 10},      # pwm 값 누락
    {"seq": 1, "exp": 30, "act": "pwm", "pwm": 101, "dur": 10},
])
def test_cmd_rejected_no_ack_no_state_change(bad):
    d = make()
    assert d.handle_cmd({"type": "CMD", **bad}) is None
    assert d.stats.cmd_rejected == 1 and d.current_override() is None
    assert validate_cmd(bad) is not None


def test_cmd_duplicate_seq_acks_again_but_does_not_reapply():
    d = make()
    d.handle_cmd({"type": "CMD", "seq": 5, "exp": 30, "act": "off", "dur": 100})
    first = d.current_override()
    ack = d.handle_cmd({"type": "CMD", "seq": 5, "exp": 30, "act": "on", "dur": 100})  # 같은 seq, 다른 내용
    assert ack["result"] == "OK"
    assert d.current_override() is first  # 재실행 없음


def test_override_layers_priority_device_over_group_over_all():
    d = make()
    d.handle_cmd({"type": "CMD", "seq": 1, "exp": 30, "act": "off", "dur": 100}, layer="all")
    d.handle_cmd({"type": "CMD", "seq": 2, "exp": 30, "act": "on", "dur": 100}, layer="group")
    assert d.current_override().act == "on"
    d.handle_cmd({"type": "CMD", "seq": 3, "exp": 30, "act": "pwm", "pwm": 10, "dur": 100}, layer="device")
    assert d.current_override().act == "pwm"
    d.handle_cmd({"type": "CMD", "seq": 4, "exp": 30, "act": "auto", "dur": 1}, layer="device")
    assert d.current_override().act == "on"  # 개별 만료 → 그룹으로


def test_override_expires_after_dur(monkeypatch):
    import tools.sim.device as mod
    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    d = make()
    d.handle_cmd({"type": "CMD", "seq": 1, "exp": 30, "act": "off", "dur": 60})
    assert d.current_override() is not None
    now[0] += 61
    assert d.current_override() is None and d.build_tm()["md"] == 0
