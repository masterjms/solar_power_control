from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.core.presence import collapse_transitions, is_online, online_clause, online_window_sec

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)


def dev(**kw):
    base = dict(online=True, state="ACTIVE", last_seen_at=NOW - dt.timedelta(seconds=60))
    base.update(kw)
    return SimpleNamespace(**base)


def test_window_active_uses_effective_ti_times_factor():
    assert online_window_sec("ACTIVE", 600, factor=3, pending_sec=4200) == 1800
    assert online_window_sec("ACTIVE", 300, factor=3, pending_sec=4200) == 900


def test_window_non_active_uses_pending_sec():
    for state in ("PENDING", "SUSPENDED", "REJECTED", "RETIRED"):
        assert online_window_sec(state, 60, factor=3, pending_sec=4200) == 4200


def test_flag_and_recency_both_required():
    assert is_online(dev(), NOW, 600, factor=3) is True
    assert is_online(dev(online=False), NOW, 600, factor=3) is False          # A 실패
    assert is_online(dev(last_seen_at=None), NOW, 600, factor=3) is False     # C 실패
    late = NOW - dt.timedelta(seconds=1801)
    assert is_online(dev(last_seen_at=late), NOW, 600, factor=3) is False     # 창 밖
    assert is_online(dev(last_seen_at=NOW - dt.timedelta(seconds=1799)), NOW, 600, factor=3)


def test_pending_uses_70min_window():
    d = dev(state="PENDING", last_seen_at=NOW - dt.timedelta(minutes=60))
    assert is_online(d, NOW, 600, factor=3, pending_sec=4200) is True
    d = dev(state="PENDING", last_seen_at=NOW - dt.timedelta(minutes=71))
    assert is_online(d, NOW, 600, factor=3, pending_sec=4200) is False


def test_sql_clause_compiles_with_same_rules():
    sql = str(online_clause(NOW).compile(compile_kwargs={"literal_binds": True}))
    assert "device.online IS true" in sql
    assert "device.last_seen_at IS NOT NULL" in sql
    assert "CASE WHEN (device.state = 'ACTIVE')" in sql
    assert "coalesce(device.ti_override, (SELECT config_profile.ti" in sql
    assert "* 3" in sql and "ELSE 4200" in sql
    assert "interval '1 second'" in sql


def test_collapse_keeps_last_transition_per_uuid():
    assert collapse_transitions([("A", True), ("B", False), ("A", False)]) == {
        "B": False, "A": False}
    assert list(collapse_transitions([("A", True), ("B", False), ("A", False)])) == ["B", "A"]
    assert collapse_transitions([]) == {}
