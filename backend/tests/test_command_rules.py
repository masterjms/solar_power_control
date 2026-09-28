"""5차 COMMAND 순수 규칙 — payload·검증·topic·오늘 밤·재시도 자격·종료 판정·override (ADR-005)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app.core.command_rules import (
    CLEARED,
    CommandInvalid,
    OverrideState,
    command_topics,
    finish_result,
    off_time,
    channel_remaining,
    channels_after_ok,
    override_summary,
    override_level_for,
    remote_status,
    retry_eligible,
    tonight_seconds,
    validate_command,
)
from app.core.region_tree import Node, Tree
from app.mqtt.handlers import next_target_status
from app.mqtt.publisher import KST, command_payload, encode, kst_ts, parse_kst_ts

U = "00112233445566778899AABB"
T0 = dt.datetime(2026, 9, 27, 1, 35, 12, tzinfo=KST)


def _v(**kw):
    base = dict(act="off", ch=None, pwm=None, dur=3600, dur_preset=None, exp=None,
                default_exp=30)
    base.update(kw)
    return validate_command(**base)


# ── payload ──────────────────────────────────────────────────────────────
def test_payload_key_order_and_ts_format():
    p = command_payload(seq=12, ts=kst_ts(T0), exp=30, act="off", ch=[1, 2], dur=3600)
    assert list(p) == ["type", "seq", "ts", "exp", "act", "ch", "dur"]
    assert p == {"type": "COMMAND", "seq": 12, "ts": "260927T013512", "exp": 30, "act": "off",
                 "ch": [1, 2], "dur": 3600}
    # 사양서 §3.10.7 예시와 바이트 단위로 같다
    assert encode(p) == (b'{"type":"COMMAND","seq":12,"ts":"260927T013512","exp":30,'
                         b'"act":"off","ch":[1,2],"dur":3600}')


def test_payload_pwm_and_auto_omit_rules():
    p = command_payload(seq=13, ts="260927T013600", exp=30, act="pwm", ch=[1, 2], pwm=[70, 40],
                        dur=3600)
    assert list(p) == ["type", "seq", "ts", "exp", "act", "ch", "pwm", "dur"]
    auto = command_payload(seq=14, ts="260927T014000", exp=30, act="auto", ch=[1, 2],
                           pwm=[1, 2], dur=99)
    assert auto == {"type": "COMMAND", "seq": 14, "ts": "260927T014000", "exp": 30,
                    "act": "auto", "ch": [1, 2]}
    on = command_payload(seq=15, ts="x", exp=30, act="on", ch=[1], pwm=[50], dur=60)
    assert "pwm" not in on and on["dur"] == 60


def test_kst_ts_converts_from_utc_and_round_trips():
    utc = dt.datetime(2026, 9, 26, 16, 35, 12, tzinfo=dt.timezone.utc)
    assert kst_ts(utc) == "260927T013512"
    assert parse_kst_ts("260927T013512") == T0


# ── 검증 ─────────────────────────────────────────────────────────────────
def test_defaults():
    s = _v()
    assert (s.act, s.ch, s.pwm, s.dur, s.exp) == ("off", (1, 2), None, 3600, 30)
    assert _v(act="AUTO", dur=None).dur is None


@pytest.mark.parametrize("kw, field", [
    ({"act": "blink"}, "act"),
    ({"ch": []}, "ch"),
    ({"ch": [0]}, "ch"),
    ({"ch": [4]}, "ch"),
    ({"ch": [1, 1]}, "ch"),
    ({"act": "pwm", "pwm": None}, "pwm"),
    ({"act": "pwm", "pwm": [50]}, "pwm"),            # 길이가 ch([1,2]) 와 다름
    ({"act": "pwm", "pwm": [50, 101]}, "pwm"),
    ({"act": "pwm", "pwm": [-1, 50]}, "pwm"),
    ({"act": "on", "pwm": [50, 50]}, "pwm"),         # pwm 은 act=pwm 일 때만
    ({"dur": None}, "dur"),                          # dur·preset 둘 다 없음
    ({"dur": 60, "dur_preset": "1h"}, "dur"),        # 둘 다 있음
    ({"dur": 0}, "dur"),
    ({"dur": 86401}, "dur"),
    ({"dur": None, "dur_preset": "2h"}, "dur_preset"),
    ({"act": "auto", "dur": 60}, "dur"),
    ({"act": "auto", "dur": None, "dur_preset": "1h"}, "dur"),
    ({"exp": 0}, "exp"),
])
def test_validation_matrix(kw, field):
    with pytest.raises(CommandInvalid) as e:
        _v(**kw)
    assert e.value.field == field


def test_valid_variants():
    assert _v(act="pwm", ch=[2], pwm=[0]).pwm == (0,)
    assert _v(ch=[3, 1]).ch == (3, 1)
    assert _v(dur=1).dur == 1 and _v(dur=86400).dur == 86400
    assert _v(dur=None, dur_preset="30m").dur == 1800
    assert _v(dur=None, dur_preset="3h").dur == 10800
    t = _v(dur=None, dur_preset="tonight")
    assert t.dur is None and t.needs_tonight
    assert _v(exp=60).exp == 60


# ── topic ────────────────────────────────────────────────────────────────
def _tree() -> Tree:
    return Tree([
        Node(1, None, "sido", "경기도"),
        Node(2, 1, "sigungu", "안양시 만안구"),
        Node(3, 2, "dong", "석수동", "4117110600", 37.41, 126.89),
        Node(4, 2, "dong", "안양동", "4117110100", 37.40, 126.92),
        Node(5, 1, "sigungu", "군포시"),
        Node(6, None, "sido", "빈 시도"),
    ])


def test_topics_device_leaf_nonleaf_all():
    tree = _tree()
    assert command_topics("device", U, tree) == [f"iotlight/device/{U}/cmd"]
    assert command_topics("node", "3", tree) == ["iotlight/group/411711060000/cmd"]
    # 상위 노드 = 하위 말단마다 1회, bjd 순
    assert command_topics("node", "2", tree) == [
        "iotlight/group/411711010000/cmd", "iotlight/group/411711060000/cmd"]
    assert command_topics("node", "1", tree) == command_topics("node", "2", tree)
    assert command_topics("node", "5", tree) == []          # 말단 없는 시군구
    assert command_topics("all", None, tree) == ["iotlight/all/cmd"]


def test_tree_paths_and_coords():
    tree = _tree()
    assert tree.path_name(4) == "경기도 > 안양시 만안구 > 안양동"
    assert tree.get(3).grp == "411711060000" and tree.get(2).grp is None
    assert tree.coords_for(2) == (37.40, 126.92)   # 자기 좌표 없음 → 첫 말단(bjd 순)
    assert tree.coords_for(5) is None
    assert sorted(tree.subtree_ids(2)) == [2, 3, 4]


def test_override_level_mapping():
    assert override_level_for("device") == "device"
    assert override_level_for("node") == "group"
    assert override_level_for("all") == "all"


# ── 오늘 밤 ──────────────────────────────────────────────────────────────
SEOUL = (37.5665, 126.9780)


def test_off_time_is_morning_in_seoul_september():
    t = off_time(dt.date(2026, 9, 27), *SEOUL)
    assert t.hour == 6  # 소등 = 일출 쪽. 9월 말 서울 06:2x


@pytest.mark.parametrize("hour", [0, 3, 6, 7, 12, 18, 20, 23])
def test_tonight_seconds_bounds(hour):
    now = dt.datetime(2026, 9, 27, hour, 0, tzinfo=KST)
    sec = tonight_seconds(now, *SEOUL)
    assert 1 <= sec <= 86400
    end = now + dt.timedelta(seconds=sec)
    assert end.hour == 6  # 언제 누르든 다음 아침 소등에서 끝난다


def test_tonight_evening_is_next_morning():
    now = dt.datetime(2026, 9, 27, 20, 0, tzinfo=KST)
    sec = tonight_seconds(now, *SEOUL)
    off = off_time(dt.date(2026, 9, 28), *SEOUL)
    expected = dt.datetime.combine(dt.date(2026, 9, 28), off, tzinfo=KST) - now
    assert sec == int(expected.total_seconds())
    # 새벽이면 오늘 아침
    early = dt.datetime(2026, 9, 27, 3, 0, tzinfo=KST)
    assert tonight_seconds(early, *SEOUL) < 4 * 3600


def test_tonight_accepts_utc_now():
    utc = dt.datetime(2026, 9, 27, 11, 0, tzinfo=dt.timezone.utc)  # KST 20:00
    assert tonight_seconds(utc, *SEOUL) == tonight_seconds(utc.astimezone(KST), *SEOUL)


# ── 재시도 자격 ──────────────────────────────────────────────────────────
def _elig(**kw):
    base = dict(status="pending", attempts=1, last_sent_at=T0, cmd_sent_at=T0, cmd_dur=3600,
                cmd_finished=False, now=T0 + dt.timedelta(seconds=60), max_attempts=3,
                retry_min_sec=20, timeout_sec=900)
    base.update(kw)
    return retry_eligible(**base)


def test_retry_eligibility():
    assert _elig()
    assert _elig(status="EXPIRED")
    for status in ("OK", "LOCAL", "BAD", "STATE"):
        assert not _elig(status=status)
    assert not _elig(attempts=3)
    assert not _elig(cmd_finished=True)
    assert not _elig(superseded=True)
    assert not _elig(now=T0 + dt.timedelta(seconds=10))           # RETRY_MIN 안
    assert not _elig(now=T0 + dt.timedelta(seconds=3600))         # sent_at + dur 지남
    assert _elig(cmd_dur=None, now=T0 + dt.timedelta(seconds=899))  # auto → TIMEOUT 기준
    assert not _elig(cmd_dur=None, now=T0 + dt.timedelta(seconds=900))
    assert _elig(last_sent_at=None)


# ── 종료 판정 ────────────────────────────────────────────────────────────
def _fin(counts, total, elapsed=10, exhausted=0):
    return finish_result(counts=counts, expired_exhausted=exhausted, total=total,
                         elapsed_sec=elapsed, timeout_sec=900)


def test_finish_result():
    assert _fin({"OK": 3}, 3) == "OK"
    assert _fin({"OK": 2, "LOCAL": 1}, 3) == "PARTIAL"
    assert _fin({"OK": 2, "pending": 1}, 3) is None
    assert _fin({"OK": 2, "EXPIRED": 1}, 3) is None                 # 아직 재시도 여지
    assert _fin({"OK": 2, "EXPIRED": 1}, 3, exhausted=1) == "PARTIAL"
    assert _fin({"BAD": 1}, 1) == "PARTIAL"
    assert _fin({"pending": 5}, 5, elapsed=900) == "TIMEOUT"
    assert _fin({"pending": 4, "EXPIRED": 1}, 5, elapsed=901) == "PARTIAL"
    assert _fin({"pending": 4, "OK": 1}, 5, elapsed=901) == "PARTIAL"
    assert _fin({}, 0, elapsed=5) is None
    assert _fin({}, 0, elapsed=900) == "TIMEOUT"


def test_next_target_status_never_downgrades_ok():
    assert next_target_status("pending", "EXPIRED") == "EXPIRED"
    assert next_target_status("EXPIRED", "OK") == "OK"
    assert next_target_status("OK", "EXPIRED") == "OK"


def test_local_is_terminal_never_applied_later():
    """2026-09-27 개정(§3.10.8·§3.10.11): LOCAL 은 버림 — 현장 조작이 끝나도 적용되지 않는다."""
    assert next_target_status("pending", "LOCAL") == "LOCAL"
    assert next_target_status("LOCAL", "OK") == "LOCAL"
    assert next_target_status("LOCAL", "EXPIRED") == "LOCAL"


# ── override ─────────────────────────────────────────────────────────────
NOW = T0 + dt.timedelta(seconds=30)


def _ch(act, seq, until, level="device"):
    return {"act": act, "level": level, "seq": seq, "until": until.isoformat()}


def test_channels_ok_sets_until_from_ts_plus_dur_per_channel():
    new = channels_after_ok(None, act="off", level="group", seq=57, sent_at=T0, dur=3600,
                            ch=[1, 2], now=NOW)
    until = T0 + dt.timedelta(seconds=3600)
    assert new == {"1": _ch("off", 57, until, "group"), "2": _ch("off", 57, until, "group")}
    assert override_summary(new, NOW) == OverrideState("off", "group", 57, until)


def test_channels_later_command_wins_regardless_of_level():
    """F/W 2026-09-27-9: 개별 > 그룹 > 전체 계층 폐기. 나중 명령이 이긴다(경로 무관)."""
    dev = {"1": _ch("on", 50, NOW + dt.timedelta(hours=1), "device")}
    new = channels_after_ok(dev, act="off", level="all", seq=51, sent_at=T0, dur=60, ch=[1], now=NOW)
    assert new["1"]["act"] == "off" and new["1"]["level"] == "all"
    # 늦게 도착한 옛 seq 의 OK(재시도) 는 더 새 명령을 덮지 않는다
    assert channels_after_ok(new, act="on", level="device", seq=50, sent_at=T0, dur=600,
                             ch=[1], now=NOW) is None


def test_channels_other_channel_untouched_and_auto_clears_only_its_channels():
    base = channels_after_ok(None, act="on", level="device", seq=60, sent_at=T0, dur=3600,
                             ch=[1], now=NOW)
    both = channels_after_ok(base, act="off", level="group", seq=61, sent_at=T0, dur=600,
                             ch=[2], now=NOW)
    assert both["1"]["act"] == "on" and both["2"]["act"] == "off"      # 주등 그대로
    only1 = channels_after_ok(both, act="auto", level="all", seq=62, sent_at=T0, dur=None,
                              ch=[2], now=NOW)
    assert set(only1) == {"1"}
    assert channels_after_ok(only1, act="auto", level="device", seq=63, sent_at=T0, dur=None,
                             ch=None, now=NOW) == {}                    # ch 없음 = 모든 채널
    assert channels_after_ok(None, act="auto", level="device", seq=64, sent_at=T0, dur=None,
                             ch=[1, 2], now=NOW) is None                 # 바꿀 것 없음


def test_channels_expired_entries_dropped_and_summary_picks_latest_until():
    old = {"1": _ch("on", 1, NOW - dt.timedelta(seconds=1))}
    new = channels_after_ok(old, act="off", level="device", seq=2, sent_at=T0, dur=60, ch=[2], now=NOW)
    assert set(new) == {"2"}
    m = {"1": _ch("on", 5, NOW + dt.timedelta(seconds=100)), "2": _ch("off", 6, NOW + dt.timedelta(seconds=900))}
    assert override_summary(m, NOW).seq == 6
    assert override_summary({}, NOW) == CLEARED
    rem = channel_remaining(m, NOW)
    assert rem["1"]["remaining_sec"] == 100 and rem["2"]["act"] == "off"


def test_remote_status():
    until = NOW + dt.timedelta(seconds=600)
    assert remote_status({"md": 2}, until, NOW) == (True, 600)
    assert remote_status({"md": 0}, until, NOW) == (False, 600)
    assert remote_status({"md": 2}, NOW - dt.timedelta(seconds=1), NOW) == (False, None)
    assert remote_status(None, None, NOW) == (False, None)
    assert remote_status({"md": "2"}, until, NOW)[0] is True


def test_payload_fits_at_buffer():
    p = command_payload(seq=4_294_967_295, ts="260927T013512", exp=3600, act="pwm",
                        ch=[1, 2, 3], pwm=[100, 100, 100], dur=86400)
    assert len(json.dumps(p, separators=(",", ":"))) < 150
