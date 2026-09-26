from __future__ import annotations

from app.mqtt.handlers import dedup_key, is_telemetry_type, normalize_type, parse_payload

UUID = "00112233445566778899AABB"


def test_parse_payload():
    assert parse_payload(b'{"type":"TM","sq":1}') == {"type": "TM", "sq": 1}
    assert parse_payload(b"") is None
    assert parse_payload(b"not json") is None
    assert parse_payload(b"[1,2]") is None
    assert parse_payload(b"\xff\xfe") is None


def test_normalize_legacy_t_key():
    data, legacy = normalize_type({"t": "TM", "sq": 3})
    assert legacy is True
    assert data == {"type": "TM", "sq": 3}
    assert "t" not in data


def test_normalize_keeps_type_when_present():
    data, legacy = normalize_type({"type": "TM", "t": "ignored"})
    assert legacy is False
    assert data["type"] == "TM"


def test_normalize_without_either_key():
    data, legacy = normalize_type({"sq": 1})
    assert legacy is False
    assert "type" not in data


def test_dedup_key_shape_and_stability():
    payload = {"type": "PONG", "seq": 7, "uuid": UUID}
    key = dedup_key(UUID, "PONG", 7, payload)
    assert key.startswith(f"{UUID}:PONG:7:")
    assert len(key.rsplit(":", 1)[1]) == 16
    # 키 순서가 달라도 같은 내용이면 같은 키 (QoS1 재전송은 바이트가 같다)
    assert key == dedup_key(UUID, "PONG", 7, {"uuid": UUID, "seq": 7, "type": "PONG"})
    # 내용이 다르면 다른 키 (같은 seq 의 두 번째 결과는 남긴다)
    assert key != dedup_key(UUID, "PONG", 7, {**payload, "extra": 1})


def test_dedup_key_without_marker():
    assert dedup_key(UUID, "EV", None, {"a": 1}).startswith(f"{UUID}:EV:-:")


def test_telemetry_type_accepts_new_and_legacy():
    assert is_telemetry_type("TELEMETRY")
    assert is_telemetry_type("TM")
    assert not is_telemetry_type("REGISTER")
    assert not is_telemetry_type("")
    # 1.0.0: {"t":"TM"} → normalize 뒤 TM
    data, legacy = normalize_type({"t": "TM", "sq": 1})
    assert legacy and is_telemetry_type(data["type"])
