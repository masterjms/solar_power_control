from __future__ import annotations

import json

import pytest

from app.constants import MQTT_MAX_PAYLOAD_BYTES
from app.errors import PayloadTooLarge
from app.mqtt.publisher import (
    check_size,
    config_set_payload,
    encode,
    ping_payload,
    register_ack_payload,
)


def test_config_set_payload_full_values():
    p = config_set_payload(cv=3, ti=600, ka=300, lat=37.3617, lon=126.9352)
    assert p == {"type": "CONFIG_SET", "cv": 3, "ti": 600, "ka": 300,
                 "lat": 37.3617, "lon": 126.9352}
    assert list(p) == ["type", "cv", "ti", "ka", "lat", "lon"]


def test_config_set_payload_always_has_cv_ti_ka():
    p = config_set_payload(cv=1, ti=300, ka=120, lat=None, lon=None)
    assert p == {"type": "CONFIG_SET", "cv": 1, "ti": 300, "ka": 120}
    # 하나만 있으면 둘 다 뺀다 — 반쪽 좌표는 지도에 못 찍는다
    assert "lat" not in config_set_payload(cv=1, ti=300, ka=120, lat=37.0, lon=None)


def test_config_set_payload_refuses_cv_zero():
    with pytest.raises(ValueError):
        config_set_payload(cv=0, ti=600, ka=300, lat=None, lon=None)


def test_encode_is_compact_and_small():
    raw = encode(config_set_payload(cv=65535, ti=3600, ka=1800, lat=-37.123456, lon=-126.123456))
    assert b" " not in raw
    assert len(raw) < 110
    assert json.loads(raw)["type"] == "CONFIG_SET"


def test_ping_and_register_ack():
    assert ping_payload(seq=1) == {"type": "PING", "seq": 1}
    ack = register_ack_payload(uuid="A" * 24, state="ACTIVE", site="A-12")
    assert ack == {"type": "REGISTER_ACK", "uuid": "A" * 24, "state": "ACTIVE", "site": "A-12"}
    assert "cv" not in ack and "grp" not in ack and "ti" not in ack
    rej = register_ack_payload(uuid="A" * 24, state="REJECTED", reason="unknown device")
    assert rej["reason"] == "unknown device" and "site" not in rej
    pend = register_ack_payload(uuid="A" * 24, state="PENDING")
    assert pend == {"type": "REGISTER_ACK", "uuid": "A" * 24, "state": "PENDING"}


def test_check_size_limits():
    check_size("t", b"x" * MQTT_MAX_PAYLOAD_BYTES)  # 경계 통과
    with pytest.raises(PayloadTooLarge) as exc:
        check_size("t", b"x" * (MQTT_MAX_PAYLOAD_BYTES + 1))
    assert exc.value.detail["limit_bytes"] == 384
