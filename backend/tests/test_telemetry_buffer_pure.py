"""TelemetryBuffer 의 DB 없는 부분 — 행 변환, sq 판정 묶음, discard."""

from __future__ import annotations

import datetime as dt

from app.mqtt.telemetry_buffer import TelemetryBuffer, telemetry_row

UUID = "00112233445566778899AABB"
NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)
TM = {"type": "TM", "sq": 41, "ts": "260924T2103", "fw": "1.0.0", "ss": 15, "cv": 3,
      "er": 0, "on": 1, "md": 0, "pw": [70, 64, 64],
      "bv": 2612, "bi": -150, "sc": 87, "pp": 3400, "li": 230, "cs": 3073}


def test_telemetry_row_maps_all_columns():
    row = telemetry_row(UUID, TM, NOW)
    assert row["uuid"] == UUID and row["received_at"] == NOW
    assert row["ts_device"] == "260924T2103"
    assert (row["sq"], row["fw"], row["ss"], row["cv"]) == (41, "1.0.0", 15, 3)
    assert (row["on"], row["md"], row["pw1"], row["pw2"], row["pw3"]) == (1, 0, 70, 64, 64)
    assert (row["bv"], row["bi"], row["sc"], row["pp"], row["li"], row["cs"]) == (
        2612, -150, 87, 3400, 230, 3073)
    assert row["raw"] is TM


def test_telemetry_row_tolerates_missing_and_bad_values():
    row = telemetry_row(UUID, {"type": "TM", "sq": "12", "pw": [1], "bv": "x"}, NOW)
    assert row["sq"] == 12 and row["pw1"] == 1 and row["pw2"] is None and row["bv"] is None
    assert row["ts_device"] is None


def test_judge_batch_counts_lost_and_reboot_per_uuid():
    buf = TelemetryBuffer(interval_sec=1.0, max_pending=100)
    buf._last_sq[UUID] = 10
    batch = [
        (UUID, {**TM, "sq": 11}, NOW),
        (UUID, {**TM, "sq": 14}, NOW),   # 12,13 유실
        (UUID, {**TM, "sq": 0}, NOW),    # 재부팅
        (UUID, {**TM, "sq": 1}, NOW),
        ("B" * 24, {**TM, "sq": 5}, NOW),  # 처음 보는 단말
    ]
    latest, events = buf._judge_batch(batch)
    assert latest[UUID]["lost"] == 2 and latest[UUID]["reboot"] == 1
    assert latest[UUID]["sq"] == 1 and latest[UUID]["payload"]["sq"] == 1
    assert latest["B" * 24] == {"lost": 0, "reboot": 0, "payload": batch[-1][1],
                                 "received_at": NOW, "sq": 5}
    kinds = [e["kind"] for e in events]
    assert kinds == ["LOST", "REBOOT"]
    assert buf.last_sq_of(UUID) == 1 and buf.last_sq_of("B" * 24) == 5


def test_offer_and_discard():
    buf = TelemetryBuffer(interval_sec=1.0, max_pending=100)
    buf.offer(UUID, payload=TM, received_at=NOW)
    buf.offer("B" * 24, payload=TM, received_at=NOW)
    buf.offer(UUID, payload=TM, received_at=NOW)
    assert buf.pending_count == 3
    assert buf.discard(UUID) == 2
    assert buf.pending_count == 1


def test_offer_wakes_at_max_pending():
    buf = TelemetryBuffer(interval_sec=1.0, max_pending=2)
    buf.offer(UUID, payload=TM, received_at=NOW)
    assert not buf._wake.is_set()
    buf.offer(UUID, payload=TM, received_at=NOW)
    assert buf._wake.is_set()
