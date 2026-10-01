"""오늘 발전량·사용량 = 단말이 보낸 eg·eu (문제점 23번) — core/energy.from_device."""

import datetime as dt

from app.core import energy

KST = dt.timezone(dt.timedelta(hours=9))
START = dt.datetime(2026, 10, 1, 0, 0, tzinfo=KST)
NOON = dt.datetime(2026, 10, 1, 12, 0, tzinfo=KST)


def test_device_values_are_kwh_x100():
    got = energy.from_device({"er": 0, "eg": 160, "eu": 85, "yg": 200, "yu": 150}, NOON, START)
    assert got["source"] == "device" and got["no_value"] is False
    assert got["gen_wh"] == 1600.0 and got["use_wh"] == 850.0  # 160 = 1.60 kWh
    assert got["co2_g"] == round(1600.0 * energy.settings.ghg_kg_per_kwh, 2)


def test_zero_is_a_value_not_missing():
    got = energy.from_device({"er": 0, "eg": 0, "eu": 0}, NOON, START)
    assert got["gen_wh"] == 0.0 and got["use_wh"] == 0.0 and got["no_value"] is False


def test_mppt_offline_means_no_value():
    got = energy.from_device({"er": 0x0010 | 0x0001, "eg": 0, "eu": 0, "yg": 0, "yu": 0}, NOON, START)
    assert got["no_value"] is True and got["gen_wh"] is None and got["use_wh"] is None


def test_old_firmware_falls_back_to_server():
    assert energy.from_device({"er": 0, "pp": 1200, "li": 30}, NOON, START) is None
    assert energy.from_device(None, None, START) is None


def test_stale_telemetry_is_blank_for_today():
    yesterday = START - dt.timedelta(hours=3)
    got = energy.from_device({"er": 0, "eg": 160, "eu": 85}, yesterday, START)
    assert got["gen_wh"] is None and got["no_value"] is False


def test_bad_types_do_not_crash():
    got = energy.from_device({"er": "x", "eg": "12", "eu": None}, NOON, START)
    assert got["gen_wh"] == 120.0 and got["use_wh"] is None
