from __future__ import annotations

import datetime as dt

from app.tasks import partitions
from app.tasks.daily_rollup import kst_day_bounds, yesterday_kst

UTC = dt.timezone.utc


def test_partition_name_and_bounds():
    assert partitions.partition_name(2026, 9) == "telemetry_202609"
    start, end = partitions.month_bounds(2026, 12)
    assert start == dt.datetime(2026, 12, 1, tzinfo=UTC)
    assert end == dt.datetime(2027, 1, 1, tzinfo=UTC)


def test_partition_ddl():
    ddl = partitions.partition_ddl(2026, 9)
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS telemetry_202609 PARTITION OF telemetry")
    assert "FROM ('2026-09-01T00:00:00+00:00') TO ('2026-10-01T00:00:00+00:00')" in ddl


def test_wanted_partitions_current_and_next():
    assert partitions.wanted_partitions(dt.datetime(2026, 12, 31, 23, tzinfo=UTC)) == [
        (2026, 12), (2027, 1),
    ]


def test_expired_partitions_13_months():
    now = dt.datetime(2026, 9, 25, tzinfo=UTC)
    names = ["telemetry_202507", "telemetry_202508", "telemetry_202509", "telemetry_202609",
             "telemetry_202610", "other_table"]
    assert partitions.expired_partitions(names, now, 13) == ["telemetry_202507"]
    assert partitions.expired_partitions(names, now, 12) == ["telemetry_202507",
                                                             "telemetry_202508"]


def test_kst_day_bounds():
    start, end = kst_day_bounds(dt.date(2026, 9, 25))
    assert start == dt.datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 9, 25, 15, 0, tzinfo=UTC)


def test_yesterday_kst_crosses_midnight():
    # UTC 15:30 = KST 00:30 다음날 → 어제는 UTC 날짜와 같다
    assert yesterday_kst(dt.datetime(2026, 9, 25, 15, 30, tzinfo=UTC)) == dt.date(2026, 9, 25)
    assert yesterday_kst(dt.datetime(2026, 9, 25, 14, 30, tzinfo=UTC)) == dt.date(2026, 9, 24)
