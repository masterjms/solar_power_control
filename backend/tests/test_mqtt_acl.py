"""단말 ACL — 사양서 §1.1.2.2 표 그대로. acc: 1 read, 2 write, 3 readwrite, 4 subscribe."""

from __future__ import annotations

import pytest

from app.core.mqtt_acl import ACC_READ, ACC_READWRITE, ACC_SUBSCRIBE, ACC_WRITE, device_acl

U = "20363930594D50170004003A"
OTHER = "00112233445566778899AABB"


@pytest.mark.parametrize("kind", ["register", "status", "result", "event"])
def test_own_uplink_topics_write_only(kind):
    topic = f"iotlight/device/{U}/{kind}"
    assert device_acl(U, topic, ACC_WRITE)
    assert not device_acl(U, topic, ACC_READ)
    assert not device_acl(U, topic, ACC_SUBSCRIBE)
    assert not device_acl(U, topic, ACC_READWRITE)


@pytest.mark.parametrize("kind", ["cmd", "config"])
def test_own_downlink_topics_read_only(kind):
    topic = f"iotlight/device/{U}/{kind}"
    assert device_acl(U, topic, ACC_READ)
    assert device_acl(U, topic, ACC_SUBSCRIBE)
    assert not device_acl(U, topic, ACC_WRITE)
    assert not device_acl(U, topic, ACC_READWRITE)


def test_other_uuid_topics_denied():
    for kind in ("register", "status", "result", "event", "cmd", "config"):
        for acc in (ACC_READ, ACC_WRITE, ACC_SUBSCRIBE):
            assert not device_acl(U, f"iotlight/device/{OTHER}/{kind}", acc)


@pytest.mark.parametrize("topic", [
    "iotlight/#",
    "iotlight/device/+/status",
    "iotlight/device/#",
    f"iotlight/device/{U}/#",
    f"iotlight/device/{U}/+",
    "#",
    "+/device/+/status",
])
def test_wildcard_subscriptions_denied(topic):
    assert not device_acl(U, topic, ACC_SUBSCRIBE)
    assert not device_acl(U, topic, ACC_READ)


def test_group_and_all_read_only():
    assert device_acl(U, "iotlight/group/4141011000/cmd", ACC_READ)
    assert device_acl(U, "iotlight/group/4141011000/cmd", ACC_SUBSCRIBE)
    assert device_acl(U, "iotlight/group/#", ACC_SUBSCRIBE)          # pattern read group/#
    assert device_acl(U, "iotlight/group/414101040001/cmd", ACC_READ)
    assert not device_acl(U, "iotlight/group/4141011000/cmd", ACC_WRITE)
    assert not device_acl(U, "iotlight/group", ACC_READ)             # 하위 없음
    assert device_acl(U, "iotlight/all/cmd", ACC_READ)
    assert device_acl(U, "iotlight/all/cmd", ACC_SUBSCRIBE)
    assert not device_acl(U, "iotlight/all/cmd", ACC_WRITE)
    assert not device_acl(U, "iotlight/all/#", ACC_SUBSCRIBE)
    assert not device_acl(U, "iotlight/all/config", ACC_READ)


def test_root_and_shape():
    assert not device_acl(U, f"other/device/{U}/status", ACC_WRITE)
    assert not device_acl(U, f"iotlight/device/{U}/status/extra", ACC_WRITE)
    assert not device_acl(U, f"iotlight/device/{U}", ACC_WRITE)
    assert not device_acl(U, "", ACC_READ)
    assert device_acl(U, f"radio/device/{U}/status", ACC_WRITE, root="radio")


def test_non_uuid_username_denied():
    assert not device_acl("server", "iotlight/#", ACC_READ)
    assert not device_acl("", f"iotlight/device/{U}/status", ACC_WRITE)
    assert not device_acl(U.lower(), f"iotlight/device/{U.lower()}/status", ACC_WRITE)
