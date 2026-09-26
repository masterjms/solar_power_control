"""SimDevice 의 순수 부분 — UUID, HMAC 비밀번호(§1.1.2.2 시험값), REGISTER/TELEMETRY payload 모양, TM 값 모델, sq, 재접속 표."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from tools.sim.device import (KST, SQ_MOD, TEST_HMAC_KEY_HEX, SimDevice, TelemetryModel, device_password,
                              hmac_key_from_hex, kst_ts, reconnect_delay, uuid_from_index)

UUID_RE = re.compile(r"^[0-9A-F]{24}$")
TEST_KEY = bytes.fromhex(TEST_HMAC_KEY_HEX)


def make(**kw) -> SimDevice:
    return SimDevice(uuid_from_index(7, 0x0201), mode=kw.pop("mode", "2cha"), hmac_key=TEST_KEY, **kw)


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


# ── HMAC 비밀번호 (§1.1.2.2 시험값) ───────────────────────────────────────

@pytest.mark.parametrize("uuid,expected", [
    ("20363930594D50170004003A", "70e8a87fba4a997f7c24d1f98300753a"),
    ("00112233445566778899AABB", "13f271bab5a9de23c3577ced778a46b9"),
])
def test_device_password_matches_spec_vectors(uuid, expected):
    assert device_password(TEST_KEY, uuid) == expected
    assert device_password(TEST_KEY, uuid.lower()) == expected  # 입력은 대문자로 접는다
    assert len(expected) == 32 and expected == expected.lower()


def test_device_password_rejects_wrong_key_length_and_differs_per_key():
    with pytest.raises(ValueError):
        device_password(b"short", "00112233445566778899AABB")
    other = bytes.fromhex("ff" * 32)
    assert device_password(other, "00112233445566778899AABB") != device_password(TEST_KEY, "00112233445566778899AABB")


def test_hmac_key_from_hex_defaults_to_spec_test_key():
    assert hmac_key_from_hex(None) == TEST_KEY and hmac_key_from_hex("") == TEST_KEY
    assert hmac_key_from_hex(" " + TEST_HMAC_KEY_HEX.upper() + " ") == TEST_KEY
    with pytest.raises(ValueError):
        hmac_key_from_hex("abcd")


def test_username_and_password_rules_by_mode():
    u = "00112233445566778899AABB"
    d = SimDevice(u, mode="2cha", hmac_key=TEST_KEY)
    assert d.username == u and d.password == "13f271bab5a9de23c3577ced778a46b9" and d.hmac
    d1 = SimDevice(u, mode="1cha")
    assert d1.username == "solarlte-test" and d1.password == "solarlte-test-2026" and not d1.hmac
    # 2cha 인데 HMAC 펌웨어 이전(공용 계정).
    d2 = SimDevice(u, mode="2cha", hmac=False)
    assert d2.username == "solarlte-test" and d2.gate.enabled and d2.tm_type == "TELEMETRY"
    # 명시 비밀번호는 그대로.
    assert SimDevice(u, mode="2cha", password="pw").password == "pw"


def test_hmac_key_from_env(monkeypatch):
    from tools.sim.device import hmac_key_from_env
    monkeypatch.setenv("MQTT_HMAC_KEY", "ab" * 32)
    assert hmac_key_from_env() == bytes.fromhex("ab" * 32)
    monkeypatch.delenv("MQTT_HMAC_KEY")
    assert hmac_key_from_env() == TEST_KEY


# ── ts ───────────────────────────────────────────────────────────────────

def test_kst_ts_format_no_offset():
    ts = kst_ts(datetime(2026, 9, 24, 21, 3, 59, tzinfo=KST))
    assert ts == "260924T2103"
    assert re.match(r"^\d{6}T\d{4}$", kst_ts())


# ── REGISTER ─────────────────────────────────────────────────────────────

def test_register_payload_shape_2cha_fw_1_4_0():
    d = make(ti=600, ka=300, cv=3, ss=15)
    p = d.build_register()
    assert p["type"] == "REGISTER" and p["uuid"] == d.uuid and p["fw"] == "1.4.0"
    assert {"uuid", "fw", "device_model", "modem_model", "msisdn", "imei", "iccid"} <= set(p)
    assert p["cv"] == 3 and p["ss"] == 15 and p["ti"] == 600 and p["ka"] == 300
    assert re.match(r"^010\d{8}$", p["msisdn"]) and p["modem_model"] == "WD-N522S"
    assert len(p["imei"]) == 15
    assert len(json.dumps(p, separators=(",", ":"))) < 384  # AT 버퍼


def test_register_payload_1cha_has_no_ka_and_empty_msisdn():
    p = SimDevice(uuid_from_index(7, 0x0201), mode="1cha").build_register()
    assert "ka" not in p and p["fw"] == "1.0.0" and p["msisdn"] == ""
    assert {"cv", "ss", "ti"} <= set(p)


def test_register_payload_legacy_omits_cv_ss_ti_ka():
    p = make(legacy_register=True).build_register()
    assert not ({"cv", "ss", "ti", "ka"} & set(p))
    assert {"uuid", "fw", "device_model", "modem_model", "msisdn", "imei", "iccid"} <= set(p)


# ── TM ───────────────────────────────────────────────────────────────────

TM_KEYS = {"sq", "ts", "fw", "ss", "cv", "er", "on", "md", "pw", "bv", "bi", "sc", "pp", "li", "cs"}


def test_tm_payload_shape_and_sq_increment():
    d = make(cv=3, ss=15)
    p0 = d.build_tm()
    p1 = d.build_tm()
    assert p0["type"] == "TELEMETRY" and "t" not in p0
    assert TM_KEYS <= set(p0)
    assert p0["sq"] == 0 and p1["sq"] == 1 and d.sq == 2
    assert p0["cv"] == 3 and p0["ss"] == 15 and p0["md"] == 0 and p0["er"] == 0 and p0["fw"] == "1.4.0"
    assert isinstance(p0["pw"], list) and len(p0["pw"]) == 3
    assert len(json.dumps(p0, separators=(",", ":"))) < 384


def test_tm_type_variants():
    assert SimDevice(uuid_from_index(7, 0x0201), mode="1cha").build_tm()["type"] == "TM"
    assert make(tm_type="TM").build_tm()["type"] == "TM"
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
    d = make(cv=2, ti=600, approval_gate=False)
    for _ in range(5):
        d.build_tm()
    d.handle_config_set({"type": "CONFIG_SET", "cv": 9, "ti": 300, "ka": 900, "lat": 37.1, "lon": 127.1})
    d.handle_cmd({"type": "CMD", "seq": 1, "exp": 30, "act": "off", "dur": 100})
    d.gate.on_ack("ACTIVE")
    await d.reboot()  # 접속 전이라 소켓 절단은 no-op
    assert d.sq == 0
    assert d.current_override() is None and d.build_tm()["md"] == 0
    assert d.gate.state is None
    assert d.cv == 9 and d.ti == 300 and d.ka == 900 and d.lat == 37.1  # Flash 저장값은 남는다
    assert d.handle_cmd({"type": "CMD", "seq": 1, "exp": 30, "act": "off", "dur": 100}) is not None  # seq 기억도 소거


# ── 재접속 표 (README "단말 동작": 30초×5 → 5분×5 → 30분) ─────────────────

def test_reconnect_schedule():
    assert [reconnect_delay(i) for i in range(12)] == [30] * 5 + [300] * 5 + [1800] * 2
    assert reconnect_delay(1000) == 1800


def test_device_reconnect_delay_uses_schedule_scale_and_override():
    d = make(time_scale=10)
    assert d._next_reconnect_delay() == 3.0
    d._reconnect_attempt = 5
    assert d._next_reconnect_delay() == 30.0
    d._reconnect_attempt = 10
    assert d._next_reconnect_delay() == 180.0
    d._reconnect_delay_override = 0.5
    assert d._next_reconnect_delay() == 0.5 and d._next_reconnect_delay() == 180.0  # override 는 1회


def test_lwt_default_off_in_2cha_on_in_1cha():
    assert make()._will() is None
    assert SimDevice(uuid_from_index(7, 0x0201), mode="1cha")._will() is not None
    assert make(lwt=True)._will() is not None
