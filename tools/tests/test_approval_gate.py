"""승인 상태머신 — 사양서 §3.4 상태별 동작, §3.5 비저장, §3.6 재전송 주기, §3.3 빈 retain."""

from __future__ import annotations

import pytest

from tools.sim.device import ApprovalGate, SimDevice, uuid_from_index


def test_disabled_gate_always_allows_telemetry_and_never_resends():
    g = ApprovalGate(enabled=False)
    assert g.telemetry_allowed and not g.resend_needed
    g.on_ack("PENDING")
    assert g.telemetry_allowed  # 2차 이전 펌웨어는 승인 개념이 없다


def test_no_ack_means_no_telemetry_and_resend():
    g = ApprovalGate(enabled=True)
    assert g.state is None and not g.telemetry_allowed and g.resend_needed


@pytest.mark.parametrize("state,tm,resend", [
    ("PENDING", False, True),
    ("ACTIVE", True, False),
    ("SUSPENDED", False, False),
    ("REJECTED", False, False),
    ("RETIRED", False, False),
])
def test_state_table_section_3_4(state, tm, resend):
    g = ApprovalGate(enabled=True)
    g.on_ack(state)
    assert g.state == state
    assert g.telemetry_allowed is tm
    assert g.resend_needed is resend


def test_unknown_state_ignored():
    g = ApprovalGate(enabled=True)
    g.on_ack("ACTIVE")
    g.on_ack("BANANA")
    assert g.state == "ACTIVE"


def test_resend_schedule_5x5min_then_30min_scaled():
    g = ApprovalGate(enabled=True, time_scale=60.0)
    delays = []
    for _ in range(7):
        delays.append(g.next_resend_delay())
        g.on_register_sent()
    assert delays == [5.0] * 5 + [30.0] * 2
    assert ApprovalGate(enabled=True).next_resend_delay() == 300.0


def test_reconnect_resets_counter_and_state():
    g = ApprovalGate(enabled=True, time_scale=1.0)
    for _ in range(6):
        g.on_register_sent()
    g.on_ack("ACTIVE")
    assert g.next_resend_delay() == 1800.0
    g.reset()
    assert g.state is None and g.resend_count == 0 and g.next_resend_delay() == 300.0


def test_empty_retain_returns_to_no_response_at_30min():
    g = ApprovalGate(enabled=True, time_scale=60.0)
    g.on_register_sent()
    g.on_ack("RETIRED")
    assert g.silenced and not g.resend_needed
    g.on_ack(None)  # 빈 payload retain = 정리
    assert g.state is None and not g.silenced and g.resend_needed
    assert g.next_resend_delay() == 30.0  # 30분 주기(§3.3)


def test_device_register_ack_handling_and_uuid_check():
    d = SimDevice(uuid_from_index(3, 0x0301), password="pw", approval_gate=True, time_scale=60)
    d.handle_register_ack({"type": "REGISTER_ACK", "uuid": d.uuid, "state": "PENDING"})
    assert d.gate.state == "PENDING"
    d.handle_register_ack({"type": "REGISTER_ACK", "uuid": "F" * 24, "state": "ACTIVE"})  # 남의 ACK
    assert d.gate.state == "PENDING"
    d.handle_register_ack({"type": "REGISTER_ACK", "uuid": d.uuid, "state": "ACTIVE", "site": "A-12"})
    assert d.gate.state == "ACTIVE" and d.gate.telemetry_allowed
    d.handle_register_ack(None)
    assert d.gate.state is None and d.stats.register_ack_rx == 4


async def test_send_tm_now_suppressed_before_active():
    d = SimDevice(uuid_from_index(4, 0x0301), password="pw", approval_gate=True)
    assert await d.send_tm_now() is None
    assert d.stats.tm_suppressed == 1 and d.sq == 0  # 억제된 건은 sq 를 쓰지 않는다
    d.gate.on_ack("ACTIVE")
    # 접속 전이라 발행은 실패하지만 payload 는 만들어지고 sq 가 소비된다.
    p = await d.send_tm_now()
    assert p is not None and p["sq"] == 0 and d.sq == 1 and d.stats.tm_sent == 0


async def test_reboot_does_not_persist_approval():
    d = SimDevice(uuid_from_index(5, 0x0301), password="pw", approval_gate=True)
    d.gate.on_ack("ACTIVE")
    await d.reboot()
    assert d.gate.state is None and not d.gate.telemetry_allowed
