from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.core.presence import is_online, online_clause, online_window_sec

NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)


def dev(**kw):
    base = dict(online=False, last_telemetry_at=None, offline_at=None, ti_device=None,
                ti_server=600)
    base.update(kw)
    return SimpleNamespace(**base)


def test_window_prefers_device_ti():
    assert online_window_sec(None, 600, 3) == 1800
    assert online_window_sec(300, 600, 3) == 900


def test_online_flag_wins():
    assert is_online(dev(online=True), NOW, factor=3) is True


def test_no_telemetry_is_offline():
    assert is_online(dev(), NOW, factor=3) is False


def test_recent_telemetry_within_ti_x3():
    assert is_online(dev(last_telemetry_at=NOW - dt.timedelta(seconds=1799)), NOW, 3) is True
    assert is_online(dev(last_telemetry_at=NOW - dt.timedelta(seconds=1801)), NOW, 3) is False


def test_device_ti_shrinks_window():
    d = dev(ti_device=300, last_telemetry_at=NOW - dt.timedelta(seconds=1000))
    assert is_online(d, NOW, 3) is False


def test_lwt_after_last_telemetry_is_offline():
    last = NOW - dt.timedelta(seconds=60)
    assert is_online(dev(last_telemetry_at=last, offline_at=NOW - dt.timedelta(seconds=10)),
                     NOW, 3) is False
    # 텔레메트리가 LWT 보다 나중이면 다시 온라인
    assert is_online(dev(last_telemetry_at=last, offline_at=NOW - dt.timedelta(seconds=120)),
                     NOW, 3) is True


def test_sql_clause_compiles_with_same_rules():
    sql = str(online_clause(NOW).compile(compile_kwargs={"literal_binds": True}))
    assert "device.online IS true" in sql
    assert "coalesce(device.ti_device, device.ti_server)" in sql
    assert "interval '1 second'" in sql
    assert "device.offline_at < device.last_telemetry_at" in sql
