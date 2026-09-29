"""ADR-009 알람 재조정 판정(순수 함수)."""

from __future__ import annotations

import datetime as dt

from app.core import alarm_rules as ar

T0 = dt.datetime(2026, 9, 29, 3, 0, tzinfo=dt.timezone.utc)


def test_er_kinds_ignores_reserved_and_lte_offline():
    assert ar.er_kinds(0) == [] and ar.er_kinds(None) == []
    assert set(ar.er_kinds(0x0004 | 0x0001)) == {"LED_FAULT", "BATT_LOW"}
    # 0x0008 예약, 0x0040 LTE_OFFLINE 은 알람이 아니다(§16.6.2)
    assert ar.er_kinds(0x0008 | 0x0040) == []
    assert set(ar.er_kinds(0x0002 | 0x0010 | 0x0020)) == {"RTC_INVALID", "MPPT_OFFLINE",
                                                          "SCHEDULE_DEFAULT"}


def test_new_condition_opens_or_watches():
    want = {("A", "LED_FAULT"): ar.Want({"er": 4}),
            ("A", "CONFIG_MISMATCH"): ar.Want({"cv_device": 1}, hold_sec=1800)}
    p = ar.reconcile(want, [], T0)
    assert p.insert_open == [("A", "LED_FAULT", {"er": 4}, None)]
    assert p.insert_watch == [("A", "CONFIG_MISMATCH", {"cv_device": 1}, None)]
    assert not (p.close or p.touch or p.promote or p.drop_watch)


def test_watch_promotes_after_hold_else_stays():
    watch = ar.Row(1, "A", "CONFIG_MISMATCH", first_seen_at=T0, opened_at=None)
    want = {("A", "CONFIG_MISMATCH"): ar.Want({"x": 1}, hold_sec=1800)}
    p = ar.reconcile(want, [watch], T0 + dt.timedelta(seconds=1799))
    assert p.touch == [(1, {"x": 1})] and not p.promote
    p = ar.reconcile(want, [watch], T0 + dt.timedelta(seconds=1800))
    assert p.promote == [(1, {"x": 1})]


def test_gone_condition_closes_open_and_drops_watch():
    rows = [ar.Row(1, "A", "LED_FAULT", T0, T0), ar.Row(2, "A", "LOCAL_OPERATION", T0, None)]
    p = ar.reconcile({}, rows, T0 + dt.timedelta(minutes=5))
    assert p.close == [1]          # 열린 것 → 이력
    assert p.drop_watch == [2]     # 연 적 없는 관찰 → 지움(이력 아님)


def test_open_stays_one_row_and_new_after_close():
    """한 단말·한 항목 열린 1건 — 계속이면 touch 만, 닫힌 뒤 다시 생기면 새 insert."""
    open_row = ar.Row(1, "A", "OFFLINE", T0, T0)
    want = {("A", "OFFLINE"): ar.Want({"last_seen_at": "x"})}
    p = ar.reconcile(want, [open_row], T0 + dt.timedelta(minutes=1))
    assert p.touch == [(1, {"last_seen_at": "x"})] and not p.insert_open
    p = ar.reconcile(want, [], T0 + dt.timedelta(minutes=2))  # 닫힌 행은 existing 에 안 들어온다
    assert p.insert_open == [("A", "OFFLINE", {"last_seen_at": "x"}, None)]
    # 조건 시작 시각을 알면 그대로 싣는다(발생 = 끊긴 때)
    since = T0 - dt.timedelta(hours=2)
    p = ar.reconcile({("A", "OFFLINE"): ar.Want(None, since=since)}, [], T0)
    assert p.insert_open == [("A", "OFFLINE", None, since)]


def test_kinds_table_matches_spec():
    assert {k.tab for k in ar.KINDS.values()} == set(ar.TABS)
    assert ar.severity_of("OFFLINE") == ar.SEV_WARN and ar.severity_of("PENDING") == ar.SEV_INFO
    assert ar.SEVERITY_RANK[ar.SEV_WARN] < ar.SEVERITY_RANK[ar.SEV_CAUTION] < ar.SEVERITY_RANK[ar.SEV_INFO]
