"""서버 설정(문제점 14번, ADR-012) — core/server_settings 순수 부분."""

from app.core import server_settings as ss


def test_defaults_are_30s_times_2():
    r = ss.Runtime()
    assert (r.command_wait_sec, r.command_attempts, r.command_timeout_sec) == (30, 2, 60)


def test_validate_range_type_and_unknown():
    clean, errors = ss.validate({"command_wait_sec": 10, "command_attempts": 3})
    assert clean == {"command_wait_sec": 10, "command_attempts": 3} and errors == {}
    clean, errors = ss.validate({"command_wait_sec": 9, "command_attempts": 4, "nope": 1})
    assert clean == {}
    assert set(errors) == {"command_wait_sec", "command_attempts", "nope"}
    for bad in (True, "30", 30.0, None):
        assert ss.validate({"command_wait_sec": bad})[1]
    assert ss.validate({"command_wait_sec": 180})[0] == {"command_wait_sec": 180}
    assert ss.validate({"command_wait_sec": 181})[1]
    assert ss.validate({"command_attempts": 0})[1]


def test_apply_drops_stale_values_and_update_merges():
    r = ss.Runtime()
    r.apply({"command_wait_sec": 45, "command_attempts": 99, "old_key": 1})
    assert r.command_wait_sec == 45
    assert r.command_attempts == 2  # 범위 밖 옛 값 → 기본값
    r.update({"command_attempts": 3})
    assert r.command_timeout_sec == 135
    r.apply({})
    assert r.command_timeout_sec == 60


def test_every_item_has_group_and_sane_default():
    groups = {g for g, _ in ss.GROUPS}
    for it in ss.ITEMS:
        assert it.group in groups
        assert it.min <= it.default <= it.max
    assert len(ss.BY_KEY) == len(ss.ITEMS)
