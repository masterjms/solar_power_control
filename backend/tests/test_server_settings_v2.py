"""서버 설정 2차(문제점 27·29번) — 보관 기간·배출계수(scale)·통계 시작일(date)."""

import datetime as dt

from app.core import server_settings as ss


def test_retention_defaults_and_days():
    r = ss.Runtime()
    assert r.telemetry_retention_months == 3
    assert r.retention_days("event_months") == 180
    assert r.retention_days("alarm_months") == 360
    assert r.retention_days("command_months") == 180
    assert r.retention_days("settings_history_months") == 360
    assert r.retention_days("deploy_months") == 180


def test_ghg_scale():
    r = ss.Runtime()
    assert r.ghg_kg_per_kwh == 0.4781
    r.update({"ghg_g_per_kwh": 4000})
    assert r.ghg_kg_per_kwh == 0.4
    _, errors = ss.validate({"ghg_g_per_kwh": 999})
    assert errors["ghg_g_per_kwh"] == "100~2000g/kWh"


def test_stats_since_date_kind():
    r = ss.Runtime()
    assert r.stats_since is None
    clean, errors = ss.validate({"stats_since": 20261003})
    assert clean == {"stats_since": 20261003} and errors == {}
    r.update(clean)
    assert r.stats_since == dt.date(2026, 10, 3)
    assert ss.validate({"stats_since": 0})[0] == {"stats_since": 0}
    for bad in (20261332, 20260230, 12345, -1):
        assert "stats_since" in ss.validate({"stats_since": bad})[1]
    assert ss.int_of(dt.date(2026, 10, 3)) == 20261003
    assert ss.date_of(0) is None and ss.date_of(20261003) == dt.date(2026, 10, 3)


def test_every_item_consistent():
    for it in ss.ITEMS:
        assert it.min <= it.default <= it.max
        assert it.kind in ("int", "date") and it.scale >= 1
        if it.kind == "date":
            assert it.scale == 1
