"""COMMAND/COMMAND_ACK(§3.10.7, §3.10.11), override 슬롯(§3.10.8), grp 구독(§3.10.9, §3.10.10) — 펌웨어 2026-09-27-3 모델."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from tools.sim.device import (KST, SEQ_MEMORY, SUBSCRIBE_LIMIT, SimDevice, command_expired, kst_ts_sec,
                              parse_cmd_ts, uuid_from_index, valid_grp, validate_command)

GRP_A = "414101040000"
GRP_B = "411711060000"
NOON = datetime(2026, 9, 27, 12, 0, tzinfo=KST)  # 낮 — 스케줄 출력 pw 0


def make(**kw) -> SimDevice:
    """게이트를 끈 2cha 단말(명령 처리 자체만 본다). 게이트가 필요한 시험은 approval_gate=True."""
    kw.setdefault("approval_gate", False)
    return SimDevice(uuid_from_index(1, 0x0599), password="pw", **kw)


def cmd(seq: int = 1, **kw) -> dict:
    p = {"type": "COMMAND", "seq": seq, "ts": kst_ts_sec(), "exp": 30, "act": "off", "ch": [1, 2], "dur": 600}
    p.update(kw)
    return {k: v for k, v in p.items() if v is not ...}


class FakeClient:
    """aiomqtt.Client 대역 — subscribe/unsubscribe/publish 호출만 기록한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.published: list[tuple[str, dict, int]] = []

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.calls.append(("sub", topic))

    async def unsubscribe(self, topic: str) -> None:
        self.calls.append(("unsub", topic))

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        self.published.append((topic, json.loads(payload), qos))


# ── ts / 형식 ────────────────────────────────────────────────────────────

def test_kst_ts_sec_shape_and_parse_roundtrip():
    ts = kst_ts_sec(datetime(2026, 9, 27, 1, 35, 12, tzinfo=KST))
    assert ts == "260927T013512" and len(ts) == 13
    assert parse_cmd_ts(ts) == datetime(2026, 9, 27, 1, 35, 12, tzinfo=KST)
    for bad in ("260927T0135", "2609271200", "260927T013560", 260927, None, "26O927T013512"):
        assert parse_cmd_ts(bad) is None


def test_valid_grp_is_12_digits_only():
    assert valid_grp(GRP_A)
    for bad in ("4141010400", "4141010400001", "41410104000A", "", None, 414101040000, "4141/1040000"):
        assert not valid_grp(bad)


# ── 판정 행렬 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("patch", [
    {"act": "blink"}, {"act": None},
    {"dur": ...}, {"dur": 0}, {"dur": 86401}, {"dur": 90000}, {"dur": "600"}, {"dur": 1.5}, {"dur": -1},
    {"act": "pwm", "pwm": [50]},                       # ch [1,2] 인데 pwm 1개
    {"act": "pwm", "ch": ..., "pwm": [50, 40]},        # ch 없음 = 채널 3개인데 pwm 2개
    {"act": "pwm", "pwm": [50, 101]}, {"act": "pwm", "pwm": [50, -1]}, {"act": "pwm", "pwm": [50, "4"]},
    {"act": "pwm", "pwm": ...},
    {"ch": [4]}, {"ch": []}, {"ch": [1, 1]}, {"ch": "1"}, {"ch": [0, 1]}, {"ch": [True]},
    {"exp": ...}, {"exp": -1}, {"exp": "30"},
    {"seq": ...}, {"seq": -1}, {"seq": "12"},
    {"ts": "2609271200"}, {"ts": "260927T1200"}, {"ts": 260927},
])
def test_bad_matrix_acks_bad_and_changes_nothing(patch):
    d = make()
    ack = d.handle_command(cmd(**patch))
    assert ack["type"] == "COMMAND_ACK" and ack["result"] == "BAD" and ack["uuid"] == d.uuid
    assert d.current_override() is None and d.md == 0
    assert d.stats.cmd_ack["BAD"] == 1 and d.stats.cmd_rejected == 1
    assert validate_command(cmd(**patch)) is not None


@pytest.mark.parametrize("patch", [
    {}, {"act": "on"}, {"dur": 1}, {"dur": 86400}, {"ch": ...}, {"ch": [3]}, {"ch": [2, 1]},
    {"act": "pwm", "pwm": [70, 40]}, {"act": "pwm", "ch": [1], "pwm": [0]}, {"act": "pwm", "ch": ..., "pwm": [1, 2, 3]},
    {"act": "auto", "dur": ...}, {"act": "auto", "dur": 0},     # auto 는 dur 을 보지 않는다
    {"ts": ...},                                                  # ts 없으면 늦음 판정 안 함
    {"exp": 0, "ts": ...},
])
def test_valid_commands_ack_ok(patch):
    d = make()
    assert d.handle_command(cmd(**patch))["result"] == "OK"
    assert validate_command(cmd(**patch)) is None


def test_ack_shape_echoes_act_and_dur():
    d = make()
    ack = d.handle_command(cmd(seq=12, act="off", dur=3600))
    assert ack == {"type": "COMMAND_ACK", "uuid": d.uuid, "seq": 12, "result": "OK", "act": "off", "dur": 3600}
    assert list(ack) == ["type", "uuid", "seq", "result", "act", "dur"]
    ack = d.handle_command(cmd(seq=14, act="auto", dur=...))
    assert ack == {"type": "COMMAND_ACK", "uuid": d.uuid, "seq": 14, "result": "OK", "act": "auto"}


# ── EXPIRED ─────────────────────────────────────────────────────────────

def test_expired_when_rtc_minus_ts_exceeds_exp():
    d = make()
    rtc = datetime(2026, 9, 27, 1, 36, 0, 900000, tzinfo=KST)  # RTC 는 초 단위로 자른다
    ts = lambda sec: (rtc.replace(microsecond=0) - timedelta(seconds=sec)).strftime("%y%m%dT%H%M%S")  # noqa: E731
    assert d.handle_command(cmd(seq=1, ts=ts(30)), rtc_now=rtc)["result"] == "OK"        # 같으면 유효
    assert d.handle_command(cmd(seq=2, ts=ts(31)), rtc_now=rtc)["result"] == "EXPIRED"
    assert d.handle_command(cmd(seq=3, ts=ts(-600)), rtc_now=rtc)["result"] == "OK"      # 미래 ts 는 늦음 아님
    assert d.handle_command(cmd(seq=4, ts=ts(100), exp=120), rtc_now=rtc)["result"] == "OK"
    assert d.handle_command(cmd(seq=5, ts=..., exp=1), rtc_now=rtc + timedelta(days=3))["result"] == "OK"
    assert command_expired(cmd(ts=ts(31)), rtc) and not command_expired(cmd(ts=...), rtc)


def test_expired_changes_nothing_and_is_not_remembered_so_retry_with_new_ts_applies():
    """ADR-005 — 재시도는 같은 seq·새 ts. EXPIRED 를 기억하면 재시도가 영영 안 먹는다."""
    d = make(clock_skew_sec=120)
    first = cmd(seq=57, act="on")
    assert d.handle_command(first)["result"] == "EXPIRED"
    assert d.current_override() is None and d.stats.cmd_ack["EXPIRED"] == 1
    d.clock_skew_sec = 0
    retry = dict(first, ts=kst_ts_sec())
    assert d.handle_command(retry)["result"] == "OK" and d.effective_act == "on"
    assert d.stats.cmd_dup == 0


def test_bad_header_wins_over_expired_but_expired_wins_over_bad_body():
    """판정 순서: ts/exp 모양 → EXPIRED → act/dur/ch/pwm → STATE → LOCAL."""
    d = make(clock_skew_sec=300)
    assert d.handle_command(cmd(seq=1, exp="x"))["result"] == "BAD"
    assert d.handle_command(cmd(seq=2, act="blink"))["result"] == "EXPIRED"


# ── STATE / LOCAL ───────────────────────────────────────────────────────

def test_state_when_not_active_and_retry_after_approval_applies():
    d = make(approval_gate=True)
    for state in (None, "PENDING", "SUSPENDED"):
        d.gate.state = state
        assert d.handle_command(cmd(seq=7))["result"] == "STATE"
    assert d.current_override() is None
    d.gate.on_ack("ACTIVE")
    assert d.handle_command(cmd(seq=7))["result"] == "OK"  # STATE 는 기억하지 않는다
    assert d.handle_command(cmd(seq=8, act="blink"))["result"] == "BAD"


def test_expired_and_bad_are_decided_before_state():
    d = make(approval_gate=True, clock_skew_sec=100)
    d.gate.state = "PENDING"
    assert d.handle_command(cmd(seq=1))["result"] == "EXPIRED"
    d.clock_skew_sec = 0
    assert d.handle_command(cmd(seq=2, dur=0))["result"] == "BAD"


def test_local_mode_acks_local_and_end_local_applies_still_valid_ones():
    d = make(local_mode=True)
    t0 = 1000.0
    assert d.handle_command(cmd(seq=1, act="off", dur=5), now=t0)["result"] == "LOCAL"
    assert d.handle_command(cmd(seq=2, act="on", dur=600), level="group", now=t0)["result"] == "LOCAL"
    assert d.md == 1 and d.effective_act == "local"
    assert d.handle_command(cmd(seq=1, act="on"), now=t0)["result"] == "LOCAL"  # 중복 → 첫 결과
    applied = d.end_local(now=t0 + 10)                     # seq 1 은 dur 5 가 지나 무효
    assert applied == [2] and d.stats.local_applied == 1
    slots = d.active_slots(now=t0 + 10)
    assert set(slots) == {"group"} and slots["group"].expires_at == t0 + 600  # 만료 = 받은 시각 + dur
    assert d.current_override(now=t0 + 10).act == "on" and not d.local_mode


def test_local_pending_auto_clears_on_end_local():
    d = make()
    d.handle_command(cmd(seq=1, act="off", dur=600), now=0.0)
    d.local_mode = True
    assert d.handle_command(cmd(seq=2, act="auto", dur=...), now=1.0)["result"] == "LOCAL"
    assert d.end_local(now=2.0) == [2] and d.current_override(now=2.0) is None


# ── 중복 seq (최근 8개) ──────────────────────────────────────────────────

def test_duplicate_seq_returns_first_result_without_reapplying():
    d = make()
    first = d.handle_command(cmd(seq=5, act="off", dur=100))
    slot = d.current_override()
    again = d.handle_command(cmd(seq=5, act="on", dur=999, ts=...))  # 같은 seq, 다른 내용
    assert again == first and d.current_override() is slot and d.stats.cmd_dup == 1
    bad = d.handle_command(cmd(seq=6, dur=0))
    assert d.handle_command(cmd(seq=6, dur=100)) == bad  # BAD 도 첫 결과로


def test_seq_memory_keeps_last_8_only():
    d = make()
    for seq in range(1, SEQ_MEMORY + 2):          # 1..9 → 1 은 밀려난다
        d.handle_command(cmd(seq=seq, act="off", dur=100))
    assert d.handle_command(cmd(seq=SEQ_MEMORY + 1, act="on"))["act"] == "off"   # 9 는 기억
    assert d.stats.cmd_dup == 1
    assert d.handle_command(cmd(seq=1, act="on"))["act"] == "on"                # 1 은 잊음 → 다시 실행
    assert d.effective_act == "on" and d.stats.cmd_dup == 1
    assert len(d._seq_memory) == SEQ_MEMORY


# ── 슬롯 우선순위·만료 (§3.10.8) ──────────────────────────────────────────

def test_slot_priority_device_over_group_over_all_and_fallback():
    d = make()
    d.handle_command(cmd(seq=1, act="off", dur=100), level="all", now=0)
    d.handle_command(cmd(seq=2, act="on", dur=60), level="group", now=0)
    assert d.current_override(now=1).act == "on"
    d.handle_command(cmd(seq=3, act="pwm", pwm=[10, 20], dur=20), level="device", now=0)
    assert d.current_override(now=1).act == "pwm"
    assert d.current_override(now=21).act == "on"     # 개별 만료 → 그룹
    assert d.current_override(now=61).act == "off"    # 그룹 만료 → 전체
    assert d.current_override(now=101) is None        # 전부 만료 → 스케줄


def test_auto_clears_only_its_own_level():
    d = make()
    d.handle_command(cmd(seq=1, act="off", dur=100), level="group", now=0)
    d.handle_command(cmd(seq=2, act="on", dur=100), level="device", now=0)
    d.handle_command(cmd(seq=3, act="auto", dur=...), level="device", now=1)
    assert d.current_override(now=2).act == "off" and set(d.active_slots(now=2)) == {"group"}
    d.handle_command(cmd(seq=4, act="auto", dur=...), level="all", now=2)   # 전체 auto 는 그룹 슬롯을 안 건드린다
    assert set(d.active_slots(now=3)) == {"group"}


def test_same_level_later_command_overwrites():
    d = make()
    d.handle_command(cmd(seq=1, act="off", dur=100), level="group", now=0)
    d.handle_command(cmd(seq=2, act="on", dur=10), level="group", now=5)
    assert d.current_override(now=6).act == "on" and d.current_override(now=16) is None


def test_channel_output_per_channel_and_pw_in_telemetry(monkeypatch):
    import tools.sim.device as mod
    now = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    d = make()
    d.model.pwm = (70, 64, 64)
    d.handle_command(cmd(seq=1, act="off", ch=[1, 2], dur=100), level="group")
    d.handle_command(cmd(seq=2, act="pwm", ch=[1], pwm=[50], dur=50), level="device")
    assert d.channel_output() == {1: 50, 2: 0, 3: None}
    tm = d.build_tm(NOON)
    assert tm["md"] == 2 and tm["pw"] == [35, 0, 0] and tm["on"] == 1
    d.handle_command(cmd(seq=3, act="on", ch=[2], dur=100), level="device")   # 개별 슬롯이 ch2 로 덮임
    assert d.channel_output() == {1: 0, 2: 100, 3: None}
    assert d.build_tm(NOON)["pw"] == [0, 64, 0]
    now[0] = 101
    tm = d.build_tm(NOON)
    assert tm["md"] == 0 and tm["pw"] == [0, 0, 0] and d.slot_dump() == {}


def test_ch_absent_means_all_channels():
    d = make()
    d.handle_command(cmd(seq=1, act="on", ch=..., dur=100))
    assert d.channel_output() == {1: 100, 2: 100, 3: 100}
    assert d.build_tm(NOON)["pw"] == list(d.model.pwm)


def test_low_battery_ack_ok_but_not_lit():
    d = make()
    d.er = 0x0001
    assert d.handle_command(cmd(seq=1, act="on", dur=100))["result"] == "OK"
    tm = d.build_tm(NOON)
    assert tm["pw"] == [0, 0, 0] and tm["on"] == 0 and tm["md"] == 2 and tm["er"] & 1


async def test_reboot_clears_slots_seq_memory_pending_and_grp():
    d = make(approval_gate=True)
    d.gate.on_ack("ACTIVE")
    d.grp = GRP_A
    d.handle_command(cmd(seq=1, act="on"), level="device")
    d.handle_command(cmd(seq=2, act="off"), level="group")
    d.local_mode = True
    d.handle_command(cmd(seq=3, act="off"))
    await d.reboot()
    assert d.slot_dump() == {} and not d._seq_memory and not d._pending_local and d.grp is None
    assert d.md == 1  # 현장 조작(DIP)은 물리 스위치라 재부팅으로 안 바뀐다


# ── 계층 판별·구독 규칙 ──────────────────────────────────────────────────

def test_level_of_topic():
    d = make()
    assert d.level_of(f"iotlight/device/{d.uuid}/cmd") == "device"
    assert d.level_of(f"iotlight/group/{GRP_A}/cmd") == "group"
    assert d.level_of("iotlight/all/cmd") == "all"
    assert d.level_of(f"iotlight/device/{d.uuid}/config") is None


def _ack(state="ACTIVE", **kw):
    return {"type": "REGISTER_ACK", "uuid": uuid_from_index(1, 0x0599), "state": state, **kw}


def test_group_topic_only_when_active_with_12_digit_grp():
    d = make(approval_gate=True)
    d.handle_register_ack(_ack("PENDING", grp=GRP_A))
    assert d.grp == GRP_A and d.desired_group_topic is None          # ACTIVE 아니면 구독 안 함
    d.handle_register_ack(_ack("ACTIVE", grp=GRP_A))
    assert d.desired_group_topic == f"iotlight/group/{GRP_A}/cmd"
    d.handle_register_ack(_ack("ACTIVE", grp="4141010400"))          # 10자리 → 거절, 이전 값 유지
    assert d.grp == GRP_A and d.stats.grp_rejected == 1
    d.handle_register_ack(_ack("ACTIVE", grp=["414101040000"]))      # 배열 아님(§3.10.9)
    assert d.grp == GRP_A and d.stats.grp_rejected == 2
    d.handle_register_ack(_ack("SUSPENDED", grp=GRP_A))
    assert d.desired_group_topic is None
    d.handle_register_ack(_ack("ACTIVE", grp=""))                    # 빈 문자열 = 미배정
    assert d.grp is None and d.desired_group_topic is None
    d.handle_register_ack(_ack("ACTIVE", grp=GRP_B))
    d.handle_register_ack(_ack("ACTIVE"))                            # grp 없음 = 미배정
    assert d.grp is None
    d.handle_register_ack(_ack("ACTIVE", grp=GRP_B))
    d.handle_register_ack(None)                                      # 빈 retain
    assert d.grp is None and d.desired_group_topic is None
    one = SimDevice(uuid_from_index(2, 0x0599), mode="1cha")
    one.grp = GRP_A
    assert one.desired_group_topic is None                           # 1차 펌웨어는 그룹 없음


async def test_group_resubscribe_unsubscribes_old_first_and_respects_6_slots():
    d = make(approval_gate=True)
    fake = FakeClient()
    d._client = fake
    d._subscriptions = [d.topic("cmd"), d.topic("config"), "iotlight/all/cmd"]
    d.handle_register_ack(_ack("ACTIVE", grp=GRP_A))
    await d._sync_group_subscription()
    d.handle_register_ack(_ack("ACTIVE", grp=GRP_B))
    await d._sync_group_subscription()
    await d._sync_group_subscription()                               # 변화 없음 → 아무것도 안 함
    assert fake.calls == [("sub", f"iotlight/group/{GRP_A}/cmd"), ("unsub", f"iotlight/group/{GRP_A}/cmd"),
                          ("sub", f"iotlight/group/{GRP_B}/cmd")]
    assert d.group_topic == f"iotlight/group/{GRP_B}/cmd" and len(d.subscriptions) == 4
    assert [g for g in d.subscriptions if "/group/" in g] == [f"iotlight/group/{GRP_B}/cmd"]
    d.handle_register_ack(_ack("SUSPENDED", grp=GRP_B))
    await d._sync_group_subscription()
    assert d.group_topic is None and fake.calls[-1] == ("unsub", f"iotlight/group/{GRP_B}/cmd")
    assert d.stats.group_subscribe == 2 and d.stats.group_unsubscribe == 2
    # 한도 6개(§3.10.10) — 이미 6개면 그룹 구독이 실패한다
    d._subscriptions = [f"x/{i}" for i in range(SUBSCRIBE_LIMIT)]
    d.handle_register_ack(_ack("ACTIVE", grp=GRP_A))
    with pytest.raises(RuntimeError):
        await d._sync_group_subscription()


# ── 수신 경로 (가짜 클라이언트) ──────────────────────────────────────────

async def test_dispatch_group_command_acks_on_own_result_topic_qos1():
    d = make()
    fake = FakeClient()
    d._client = fake
    d._group_topic = f"iotlight/group/{GRP_A}/cmd"
    await d._dispatch(f"iotlight/group/{GRP_A}/cmd", json.dumps(cmd(seq=3)).encode(), False)
    assert fake.published == [(d.topic("result"), {"type": "COMMAND_ACK", "uuid": d.uuid, "seq": 3, "result": "OK",
                                                   "act": "off", "dur": 600}, 1)]
    assert d.stats.cmd_rx_by_level["group"] == 1 and d.slot_dump()["group"]["seq"] == 3
    # 지금 구독 중이 아닌 그룹(해제 직후 도착 등)은 버린다
    await d._dispatch(f"iotlight/group/{GRP_B}/cmd", json.dumps(cmd(seq=4)).encode(), False)
    assert d.stats.cmd_foreign_group == 1 and len(fake.published) == 1
    # 옛 type:"CMD" 는 모른다
    await d._dispatch(d.topic("cmd"), json.dumps(dict(cmd(seq=5), type="CMD")).encode(), False)
    assert d.stats.cmd_legacy_rx == 1 and len(fake.published) == 1


async def test_silent_commands_ignores_entirely_then_processes_retry():
    d = make(silent_commands=1)
    fake = FakeClient()
    d._client = fake
    await d._dispatch("iotlight/all/cmd", json.dumps(cmd(seq=9)).encode(), False)
    assert d.stats.cmd_silenced == 1 and not fake.published and d.stats.cmd_rx == 0 and not d._seq_memory
    await d._dispatch(d.topic("cmd"), json.dumps(cmd(seq=9, ts=kst_ts_sec())).encode(), False)
    assert fake.published[-1][1]["result"] == "OK" and d.slot_dump()["device"]["seq"] == 9
    assert len(d.commands_received(seq=9)) == 2


async def test_delay_commands_processes_late():
    d = make(delay_commands_sec=0.05)
    fake = FakeClient()
    d._client = fake
    await d._dispatch(d.topic("cmd"), json.dumps(cmd(seq=11)).encode(), False)
    assert not fake.published and d.stats.cmd_delayed == 1
    await asyncio.sleep(0.2)
    assert fake.published[-1][1]["result"] == "OK"


async def test_delay_beyond_exp_gives_expired():
    d = make(delay_commands_sec=0.05, clock_skew_sec=31)  # 지연 + 시계 = exp 30 초과
    fake = FakeClient()
    d._client = fake
    await d._dispatch(d.topic("cmd"), json.dumps(cmd(seq=12)).encode(), False)
    await asyncio.sleep(0.2)
    assert fake.published[-1][1]["result"] == "EXPIRED" and d.acks_for(12)[0]["result"] == "EXPIRED"


async def test_silent_results_still_logs_ack():
    d = make(silent_results=True)
    fake = FakeClient()
    d._client = fake
    await d._dispatch(d.topic("cmd"), json.dumps(cmd(seq=13)).encode(), False)
    assert not fake.published and d.acks_for(13)[0]["result"] == "OK"
