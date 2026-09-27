"""단말 설정 S-23(UI 명세 8장, 펌웨어 2026-09-27-7)과 추가 Telemetry(§3.10.8 개정) — 시뮬레이터 단위 시험."""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.sim import settings as st
from tools.sim.device import SimDevice, kst_ts_sec, uuid_from_index


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict, int]] = []

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        pass

    async def unsubscribe(self, topic: str) -> None:
        pass

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        self.published.append((topic, json.loads(payload), qos))

    def of(self, type_: str) -> list[dict]:
        return [p for (_, p, _) in self.published if p.get("type") == type_]


def make(state: str | None = "ACTIVE", **kw) -> SimDevice:
    d = SimDevice(uuid_from_index(3, 0x0699), password="pw", **kw)
    if state is not None:
        d.gate.on_ack(state)
    return d


def wired(state: str | None = "ACTIVE", **kw) -> tuple[SimDevice, FakeClient]:
    kw.setdefault("extra_tm_delay", 0.05)
    d = make(state, **kw)
    fake = FakeClient()
    d._client = fake  # type: ignore[assignment]
    return d, fake


def set_msg(seq: int = 10, tbl: dict | None = None, **changes) -> dict:
    v = st.defaults()
    v.update(changes)
    p = {"type": "SETTINGS_SET", "seq": seq, "v": v}
    if tbl is not None:
        p["tbl"] = tbl
    return p


def seoul(crc: str | None = st.SEOUL_TBL_CRC) -> dict:
    t = {"region": "서울", "lat_e6": 37566500, "lon_e6": 126978000, "on": 0, "off": 0}
    if crc is not None:
        t["crc"] = crc
    return t


def raw(p: dict) -> bytes:
    return st.one_line(p)


# ── 항목·지문·표 CRC (ADR-007 에서 고정한 값) ─────────────────────────────

def test_ui_items_has_25_keys_in_file_order():
    ks = st.keys()
    assert len(ks) == 25 and ks[0] == "start_ofst" and ks[-1] == "rtn_time" and len(set(ks)) == 25
    assert st.defaults()["cut24"] == 2550 and st.defaults()["fade"] == 10


def test_default_sh_is_38AF0DBD():
    assert st.settings_sh(st.defaults()) == st.DEFAULT_SH == "38AF0DBD"
    changed = dict(st.defaults(), fade=5)
    assert st.settings_sh(changed) != st.DEFAULT_SH and len(st.settings_sh(changed)) == 8


def test_sh_uses_signed_32bit_le_in_file_order():
    import struct
    import zlib
    v = dict(st.defaults(), start_ofst=-60, stop_ofst=-1)
    blob = b"".join(struct.pack("<i", v[k]) for k in st.keys())
    assert st.settings_sh(v) == "%08X" % zlib.crc32(blob)


def test_table_crc_seoul_and_busan_default():
    assert st.table_crc(37566500, 126978000, 0, 0) == "69C1DF86"
    t = st.default_tbl(ss=7)
    assert t["region"] == "부산" and t["src"] == 0 and t["ss"] == 7
    assert t["crc"] == st.table_crc(35179600, 129075600, 0, 0) and len(t["crc"]) == 8


def test_region_for_report_replaces_quote_backslash_control():
    assert st.region_for_report('a"b\\c\x01d서울') == "a?b?c?d서울"


# ── 규칙 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("changes,rule", [
    ({"cut12": 1315, "rtn12": 1315}, "batt12_order"),
    ({"cut12": 1400, "rtn12": 1300}, "batt12_order"),
    ({"cut24": 2630, "rtn24": 2630}, "batt24_order"),
    ({"stage2_h": 19}, "stage_order"),                                  # 20:00 → 19:00 → 02:00 … 24h 넘음
    ({"stage2_h": 20, "stage2_m": 0}, "stage_order"),                   # 같은 시각
    ({"stage1_h": 20, "stage2_h": 2, "stage3_h": 0, "stage4_h": 4}, "stage_order"),   # 2→3 이 거꾸로
])
def test_rules_violations(changes, rule):
    assert st.check_rules(dict(st.defaults(), **changes)) == rule


@pytest.mark.parametrize("changes", [
    {},                                                                  # 기본 20:00 00:00 02:00 04:00
    {"stage1_h": 18, "stage1_m": 30, "stage2_h": 22, "stage2_m": 45, "stage3_h": 23, "stage3_m": 50,
     "stage4_h": 23, "stage4_m": 55},                                   # 자정 안 넘김
    {"stage1_h": 23, "stage1_m": 0, "stage2_h": 1, "stage3_h": 3, "stage4_h": 22},  # 23시간
])
def test_rules_ok(changes):
    assert st.check_rules(dict(st.defaults(), **changes)) is None


# ── SETTINGS_SET 검사 행렬 ──────────────────────────────────────────────

def _drop(p: dict, key: str) -> dict:
    p["v"].pop(key)
    return p


@pytest.mark.parametrize("payload,result", [
    ({"type": "SETTINGS_SET", "seq": 1}, "BAD"),                                        # v 없음
    (_drop(set_msg(), "rtn_time"), "BAD"),                                              # 25개 중 누락
    (set_msg(fade="5"), "BAD"),                                                         # 정수 아님
    (set_msg(fade=True), "BAD"),
    (set_msg(tbl={"region": "서울", "lat_e6": 1, "lon_e6": 1, "on": 0, "crc": "00000000"}), "BAD"),  # off 없음
    (set_msg(tbl=seoul(crc=None)), "BAD"),                                              # crc 없음
    (set_msg(fade=21), "RANGE"), (set_msg(start_ofst=61), "RANGE"), (set_msg(cut24=3001), "RANGE"),
    (set_msg(manual_40w=9), "RANGE"),
    (set_msg(tbl=dict(seoul(), lat_e6=90_000_001)), "RANGE"),
    (set_msg(tbl=dict(seoul(), on=181)), "RANGE"),
    (set_msg(tbl=dict(seoul(), region="가" * 16)), "RANGE"),                            # 48바이트
    (set_msg(cut24=2700, rtn24=2600), "RULE"),
    (set_msg(stage3_h=23), "RULE"),
    (set_msg(tbl=seoul(crc="69C1DF87")), "CRC"),
    (set_msg(fade=5), "OK"), (set_msg(tbl=seoul()), "OK"), (set_msg(tbl=seoul(crc="69c1df86")), "OK"),
    (set_msg(tbl=dict(seoul(), region="가" * 15 + "ab", crc=st.SEOUL_TBL_CRC)), "OK"),  # 47바이트
])
def test_validate_settings_set_matrix(payload, result):
    assert st.validate_settings_set(payload, st.defaults())[0] == result


def test_range_wins_over_rule_and_rule_over_crc():
    assert st.validate_settings_set(set_msg(fade=0, cut12=1500, rtn12=1100), st.defaults())[0] == "RANGE"
    assert st.validate_settings_set(set_msg(cut12=1500, rtn12=1100, tbl=seoul("0")), st.defaults())[0] == "RULE"


# ── 단말 동작 ────────────────────────────────────────────────────────────

def test_settings_get_reply_shape_defaults():
    d = make("PENDING")
    r = d.handle_settings_get({"type": "SETTINGS_GET", "seq": 501})
    assert set(r) == {"type", "uuid", "seq", "sh", "v", "tbl", "dev"}
    assert r["type"] == "SETTINGS" and r["seq"] == 501 and r["sh"] == "38AF0DBD" and r["uuid"] == d.uuid
    assert list(r["v"]) == list(st.keys()) and r["v"] == st.defaults()
    assert r["tbl"] == dict(st.default_tbl(ss=d.ss)) and r["dev"] == {"dip": 8, "bat": 24}
    assert len(raw(r)) < 700   # 8.4 "가장 큰 응답 약 640B"


@pytest.mark.parametrize("state", [None, "SUSPENDED", "REJECTED", "RETIRED"])
def test_settings_get_state_when_not_pending_or_active(state):
    d = make(state)
    r = d.handle_settings_get({"type": "SETTINGS_GET", "seq": 1})
    assert r["type"] == "SETTINGS_ACK" and r["result"] == "STATE" and set(r) == {"type", "uuid", "seq", "result", "sh", "ss"}


@pytest.mark.parametrize("state", [None, "PENDING", "SUSPENDED"])
def test_settings_set_state_when_not_active(state):
    d = make(state)
    r = d.handle_settings_set(set_msg(fade=5))
    assert r["result"] == "STATE" and d.settings["fade"] == 10 and d.ss == 0


def test_settings_set_ok_applies_all_increments_ss_and_ack_shape():
    d = make(ss=31)
    r = d.handle_settings_set(set_msg(seq=502, fade=5, stage1_pwm=80))
    assert r == {"type": "SETTINGS_ACK", "uuid": d.uuid, "seq": 502, "result": "OK",
                 "sh": st.settings_sh(dict(st.defaults(), fade=5, stage1_pwm=80)), "ss": 32}
    assert d.settings["fade"] == 5 and d.ss == 32 and d.tbl["src"] == 0   # tbl 없으면 표 그대로


def test_settings_set_identical_is_ok_without_ss_increment():
    d = make(ss=4)
    r = d.handle_settings_set(set_msg())
    assert r["result"] == "OK" and r["ss"] == 4 and r["sh"] == "38AF0DBD"
    d.handle_settings_set(set_msg(tbl=seoul()))
    assert d.ss == 5
    r = d.handle_settings_set(set_msg(seq=11, tbl=seoul()))          # 같은 표 조건 다시 → 같은 값
    assert r["result"] == "OK" and d.ss == 5


def test_settings_set_tbl_stores_src_2_and_crc():
    d = make()
    d.local_save({"tbl": {"region": "군포"}})
    assert d.tbl["src"] == 1
    assert d.handle_settings_set(set_msg(tbl=seoul(crc="69c1df86")))["result"] == "OK"
    rep = d.build_settings(1)["tbl"]
    assert rep["src"] == 2 and rep["crc"] == "69C1DF86" and rep["region"] == "서울" and rep["lat_e6"] == 37566500
    assert rep["ss"] == d.ss


@pytest.mark.parametrize("payload,result", [
    (set_msg(fade=21), "RANGE"), (set_msg(cut12=1400, rtn12=1300), "RULE"),
    (_drop(set_msg(), "fade"), "BAD"), (set_msg(tbl=seoul("DEADBEEF")), "CRC"),
])
def test_settings_set_rejections_change_nothing(payload, result):
    d = make(ss=3)
    before = (dict(d.settings), dict(d.tbl), d.ss)
    r = d.handle_settings_set(payload)
    assert r["result"] == result and (d.settings, d.tbl, d.ss) == before and r["sh"] == "38AF0DBD"


def test_settings_flash_fail_keeps_old_values_and_is_separate_from_config_flash():
    d = make(settings_flash_fail_next=1, flash_fail_next=0)
    assert d.handle_settings_set(set_msg(fade=5))["result"] == "FLASH"
    assert d.settings["fade"] == 10 and d.ss == 0
    assert d.handle_settings_set(set_msg(fade=5))["result"] == "OK" and d.ss == 1


def test_local_save_increments_ss_even_without_change_and_changes_values():
    d = make(ss=10)
    d.local_save()
    assert d.ss == 11 and d.sh == "38AF0DBD"
    d.local_save({"fade": 3, "tbl": {"region": "서울", "lat_e6": 37566500, "lon_e6": 126978000}})
    assert d.ss == 12 and d.settings["fade"] == 3 and d.tbl["src"] == 1 and d.tbl["crc"] == "69C1DF86"
    with pytest.raises(KeyError):
        d.local_save({"nope": 1})


def test_initial_settings_override_defaults():
    d = make(settings={"fade": 7})
    assert d.settings["fade"] == 7 and d.sh != "38AF0DBD"


# ── 수신 경로 (가짜 클라이언트) ──────────────────────────────────────────

async def test_dispatch_settings_get_on_device_topic_only():
    d, fake = wired("PENDING")
    for t in ("iotlight/all/cmd", f"iotlight/group/414101040000/cmd"):
        await d._dispatch(t, raw({"type": "SETTINGS_GET", "seq": 1}), False)
    assert not fake.published and d.stats.settings_ignored_topic == 2
    await d._dispatch(d.topic("cmd"), raw({"type": "SETTINGS_GET", "seq": 2}), False)
    (topic, p, qos), = fake.published
    assert topic == d.topic("result") and qos == 1 and p["type"] == "SETTINGS" and p["seq"] == 2


@pytest.mark.parametrize("seq", [None, "5", -1, 1.5])
async def test_dispatch_settings_bad_seq_dropped_without_reply(seq):
    d, fake = wired()
    p = {"type": "SETTINGS_GET"}
    if seq is not None:
        p["seq"] = seq
    await d._dispatch(d.topic("cmd"), raw(p), False)
    assert not fake.published and d.stats.settings_bad_seq == 1


async def test_dispatch_drops_oversize_and_newline_payloads():
    d, fake = wired()
    big = set_msg(fade=5)
    big["pad"] = "x" * 1000
    assert len(raw(big)) > st.RX_LINE_MAX
    await d._dispatch(d.topic("cmd"), raw(big), False)
    pretty = json.dumps(set_msg(fade=5), indent=1).encode()
    await d._dispatch(d.topic("cmd"), pretty, False)
    assert not fake.published and d.stats.rx_oversize == 1 and d.stats.rx_newline == 1
    assert d.settings["fade"] == 10 and d.stats.settings_set_rx == 0 and [x[2] for x in d.rx_dropped] == ["oversize", "newline"]
    # 900B 근처(서버 상한)는 읽는다
    ok = set_msg(fade=5)
    ok["pad"] = "x" * (900 - len(raw(ok)) - 9)
    assert 880 <= len(raw(ok)) <= 900
    await d._dispatch(d.topic("cmd"), raw(ok), False)
    assert fake.of("SETTINGS_ACK")[-1]["result"] == "OK"


async def test_settings_silent_next_ignores_then_answers_new_seq():
    d, fake = wired(settings_silent_next=1)
    await d._dispatch(d.topic("cmd"), raw({"type": "SETTINGS_GET", "seq": 7}), False)
    assert not fake.published and d.stats.settings_silenced == 1
    await d._dispatch(d.topic("cmd"), raw({"type": "SETTINGS_GET", "seq": 8}), False)
    assert [p["seq"] for p in fake.of("SETTINGS")] == [8]
    assert [p["seq"] for p in d.settings_requests("SETTINGS_GET")] == [7, 8]


async def test_settings_set_ok_sends_extra_tm_after_delay_but_not_on_reject():
    d, fake = wired()
    await d._dispatch(d.topic("cmd"), raw(set_msg(fade=21)), False)
    await asyncio.sleep(0.15)
    assert d.stats.extra_tm == 0
    await d._dispatch(d.topic("cmd"), raw(set_msg(seq=11, fade=5)), False)
    assert not fake.of("TELEMETRY")
    await asyncio.sleep(0.15)
    tms = fake.of("TELEMETRY")
    assert len(tms) == 1 and tms[0]["ss"] == 1 and d.extra_tm_log[-1][1] == "settings_ok"


# ── 추가 Telemetry (§3.10.8) ─────────────────────────────────────────────

def cmd(seq: int, **kw) -> dict:
    p = {"type": "COMMAND", "seq": seq, "ts": kst_ts_sec(), "exp": 30, "act": "off", "ch": [1, 2], "dur": 600}
    p.update(kw)
    return p


async def test_command_ok_extra_tm_once_merged_and_not_for_dup_or_local():
    d, fake = wired()
    await d._dispatch(d.topic("cmd"), raw(cmd(1)), False)
    await d._dispatch(d.topic("cmd"), raw(cmd(2, act="on")), False)       # 2초 안에 겹침 → 한 건
    await asyncio.sleep(0.15)
    assert d.stats.extra_tm == 1 and d.stats.extra_tm_merged == 1 and len(fake.of("TELEMETRY")) == 1
    await d._dispatch(d.topic("cmd"), raw(cmd(2, act="on")), False)       # 같은 seq 재수신 → 없음
    await asyncio.sleep(0.15)
    assert d.stats.extra_tm == 1
    d.end_local()
    d._local_mode = True                                                   # 슬롯 취소 없이 현장 상태만
    await d._dispatch(d.topic("cmd"), raw(cmd(3)), False)                 # LOCAL → 없음
    await asyncio.sleep(0.15)
    assert d.acks_for(3)[0]["result"] == "LOCAL" and d.stats.extra_tm == 1


async def test_expiry_sends_extra_tm_and_drops_slot():
    d, fake = wired()
    await d._dispatch(d.topic("cmd"), raw(cmd(1, dur=1)), False)
    await asyncio.sleep(0.15)
    assert d.stats.extra_tm == 1 and d.md == 2
    await asyncio.sleep(1.1)
    assert d.stats.extra_tm == 2 and d.extra_tm_log[-1][1] == "expiry" and d.md == 0
    assert fake.of("TELEMETRY")[-1]["md"] == 0


async def test_replaced_or_auto_slot_does_not_fire_expiry():
    d, _ = wired(extra_tm_delay=0.01)
    await d._dispatch(d.topic("cmd"), raw(cmd(1, dur=1)), False)
    await d._dispatch(d.topic("cmd"), raw(cmd(2, act="auto", dur=None)), False)
    await asyncio.sleep(1.2)
    assert [r for (_, r, _) in d.extra_tm_log] == ["command_ok"]


async def test_start_local_with_remote_sends_md1_extra_tm():
    d, fake = wired()
    await d._dispatch(d.topic("cmd"), raw(cmd(1)), False)
    await asyncio.sleep(0.15)
    assert d.start_local() == 1
    await asyncio.sleep(0.15)
    assert d.extra_tm_log[-1][1] == "local" and fake.of("TELEMETRY")[-1]["md"] == 1
    n = d.stats.extra_tm
    d.end_local()
    assert d.start_local() == 0            # 취소할 원격이 없으면 추가 TM 없음(§3.10.8 표)
    await asyncio.sleep(0.15)
    assert d.stats.extra_tm == n


async def test_extra_tm_resets_periodic_timer():
    d, fake = wired(ti=1)
    d.extra_tm_delay = 0.6
    loop_task = asyncio.create_task(d._tm_loop())
    try:
        await asyncio.sleep(0.05)                        # 첫 TM (t=0)
        d._schedule_extra_tm("command_ok")               # t≈0.65 에 추가 TM → 다음 주기는 t≈1.65
        await asyncio.sleep(1.3)                         # t≈1.35: 주기 TM(원래 t=1.0) 이 안 나갔어야
        assert len(fake.of("TELEMETRY")) == 2
        await asyncio.sleep(0.5)                         # t≈1.85
        assert len(fake.of("TELEMETRY")) == 3
    finally:
        loop_task.cancel()
