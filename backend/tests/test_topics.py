from __future__ import annotations

from app.constants import TopicKind
from app.mqtt import topics

UUID = "00112233445566778899AABB"


def test_parse_each_kind():
    for kind in ("register", "status", "result", "event"):
        assert topics.parse_inbound(f"iotlight/device/{UUID}/{kind}") == (UUID, TopicKind(kind))


def test_parse_rejects_lowercase_and_wrong_length():
    assert topics.parse_inbound(f"iotlight/device/{UUID.lower()}/status") is None
    assert topics.parse_inbound(f"iotlight/device/{UUID[:-1]}/status") is None
    assert topics.parse_inbound(f"iotlight/device/{UUID}X/status") is None


def test_parse_rejects_other_kinds_and_roots():
    assert topics.parse_inbound(f"iotlight/device/{UUID}/cmd") is None
    assert topics.parse_inbound(f"iotlight/device/{UUID}/config") is None
    assert topics.parse_inbound(f"iotradio/device/{UUID}/status") is None
    assert topics.parse_inbound(f"iotlight/group/{UUID}/status") is None
    assert topics.parse_inbound("") is None


def test_outbound_topics():
    assert topics.device_cmd(UUID) == f"iotlight/device/{UUID}/cmd"
    assert topics.device_config(UUID) == f"iotlight/device/{UUID}/config"
    assert topics.all_cmd() == "iotlight/all/cmd"
    assert topics.group_cmd("4141011000") == "iotlight/group/4141011000/cmd"


def test_subscriptions_are_qos1_wildcards():
    subs = dict(topics.SUBSCRIPTIONS)
    assert set(subs) == {
        "iotlight/device/+/register",
        "iotlight/device/+/status",
        "iotlight/device/+/result",
        "iotlight/device/+/event",
    }
    assert all(q == 1 for q in subs.values())
