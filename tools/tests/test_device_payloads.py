"""SimDevice 의 순수 부분 — UUID, REGISTER/TM payload 모양, TM 값 모델, sq."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from tools.sim.device import KST, SQ_MOD, SimDevice, TelemetryModel, kst_ts, uuid_from_index

UUID_RE = re.compile(r"^[0-9A-F]{24}$")


def make(**kw) -> SimDevice:
    return SimDevice(uuid_from_index(7, 0x0201), mode=kw.pop("mode", "2cha"), password="pw", **kw)


# ── UUID ─────────────────────────────────────────────────────────────────

def test_uuid_from_index_is_24_upper_hex_and_deterministic():
    a, b = uuid_from_index(0), uuid_from_index(0)
    assert a == b and UUID_RE.match(a)
    assert uuid_from_index(1) != a
    assert uuid_from_index(5, 0x0203).startswith("51A00203")
    assert len({uuid_from_index(i, 3) for i in range(1000)}) == 1000


def test_uuid_from_index_rejects_out_of_range():
    with pytest.raises(ValueError):
        uuid_from_index(-1)
    with pytest.raises(ValueError):
        uuid_from_index(0, 70000)


def test_device_rejects_bad_uuid():
    with pytest.raises(ValueError):
        SimDevice("abc", password="x")
    with pytest.raises(ValueError):
        SimDevice("51a0" + "0" * 20, password="x")  # 소문자


# ── ts ───────────────────────────────────────────────────────────────────

def test_kst_ts_format_no_offset():
    ts = kst_ts(datetime(2026, 9, 24, 21, 3, 59, tzinfo=KST))
    assert ts == "260924T2103"
    assert re.match(r"^\d{6}T\d{4}$", kst_ts())


# ── REGISTER ─────────────────────────────────────────────────────────────

def test_register_payload_shape_2cha():
    d = make(ti=600, cv=3, ss=15, fw="1.0.0")
    p = d.build_register()
    assert p["type"] == "REGISTER" and p["uuid"] == d.uuid
    assert {"uuid", "fw", "device_model", "modem_model", "msisdn", "imei", "iccid"} <= set(p)
    assert p["cv"] == 3 and p["ss"] == 15 and p["ti"] == 600
    assert p["msisdn"] == "" and p["modem_model"] == "WD-N522S"
    assert len(p["imei"]) == 15
    assert len(json.dumps(p, separators=(",", ":"))) < 384  # AT 버퍼


def test_register_payload_legacy_omits_cv_ss_ti():
    p = make(legacy_register=True).build_register()
    assert not ({"cv", "ss", "ti"} & set(p))
    assert {"uuid", "fw", "device_model", "modem_model", "msisdn", "imei", "iccid"} <= set(p)


def test_username_rules_by_mode():
    u = uuid_from_index(1)
    assert SimDevice(u, mode="2cha", password="pw").username == u
    d = SimDevice(u, mode="1cha")
    assert d.username == "solarlte-test" and d.password == "solarlte-test-2026"


# ── TM ───────────────────────────────────────────────────────────────────

TM_KEYS = {"sq", "ts", "fw", "ss", "cv", "er", "on", "md", "pw", "bv", "bi", "sc", "pp", "li", "cs"}


def test_tm_payload_shape_and_sq_increment():
    d = make(cv=3, ss=15)
    p0 = d.build_tm()
    p1 = d.build_tm()
    assert p0["type"] == "TM" and "t" not in p0
    assert TM_KEYS <= set(p0)
    assert p0["sq"] == 0 and p1["sq"] == 1 and d.sq == 2
    assert p0["cv"] == 3 and p0["ss"] == 15 and p0["md"] == 0 and p0["er"] == 0
    assert isinstance(p0["pw"], list) and len(p0["pw"]) == 3
    assert len(json.dumps(p0, separators=(",", ":"))) < 384


def test_tm_legacy_t_key():
    p = make(legacy_t_key=True).build_tm()
    assert p["t"] == "TM" and "type" not in p


def test_tm_values_day_vs_night():
    model = TelemetryModel(seed=1)
    noon = model.sample(datetime(2026, 6, 21, 12, 0, tzinfo=KST))
    assert noon["pp"] > 3000 and noon["bi"] > 0 and noon["on"] == 0 and noon["pw"] == [0, 0, 0]
    assert noon["cs"] & 0x0001  # Running
    night = model.sample(datetime(2026, 6, 21, 23, 0, tzinfo=KST))
    assert night["pp"] == 0 and night["bi"] < 0 and night["on"] == 1 and night["pw"] == [70, 64, 64]
    assert night["cs"] == 0 and night["li"] > 0
    assert 0 <= night["sc"] <= 100 and 2000 < night["bv"] < 3000


def test_tm_values_are_x100_ints_not_divided():
    p = make().build_tm(datetime(2026, 6, 21, 12, 0, tzinfo=KST))
    for k in ("bv", "bi", "sc", "pp", "li", "cs"):
        assert isinstance(p[k], int)
    assert p["bv"] > 2000  # 26.xxV → 26xx


def test_tm_model_is_deterministic_per_seed():
    a = [TelemetryModel(seed=9).sample(datetime(2026, 1, 1, h, tzinfo=KST)) for h in range(24)]
    b = [TelemetryModel(seed=9).sample(datetime(2026, 1, 1, h, tzinfo=KST)) for h in range(24)]
    assert a == b


def test_drop_next_tm_skips_sq():
    d = make()
    d.build_tm()
    d.drop_next_tm(3)
    assert d.build_tm()["sq"] == 4


def test_sq_wrap_sequence():
    d = make()
    d.sq_wrap()
    assert [d.build_tm()["sq"] for _ in range(3)] == [SQ_MOD - 2, SQ_MOD - 1, 0]
    assert d.sq == 1


async def test_reboot_resets_sq_and_volatile_state_but_keeps_flash():
    d = make(cv=2, ti=600)
    for _ in range(5):
        d.build_tm()
    d.handle_config_set({"type": "CONFIG_SET", "cv": 9, "ti": 300, "lat": 37.1, "lon": 127.1})
    d.handle_cmd({"type": "CMD", "seq": 1, "exp": 30, "act": "off", "dur": 100})
    d.gate.on_ack("ACTIVE")
    await d.reboot()  # 접속 전이라 소켓 절단은 no-op
    assert d.sq == 0
    assert d.current_override() is None and d.build_tm()["md"] == 0
    assert d.gate.state is None
    assert d.cv == 9 and d.ti == 300 and d.lat == 37.1  # Flash 저장값은 남는다
    assert d.handle_cmd({"type": "CMD", "seq": 1, "exp": 30, "act": "off", "dur": 100}) is not None  # seq 기억도 소거
