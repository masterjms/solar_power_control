"""Mosquitto 로그 파서 + tail 의 파일 처리 (ADR-004). DB 는 안 만진다."""

from __future__ import annotations

from app.core.broker_log import next_offset, not_authorised, parse_chunk, parse_line
from app.core.presence import collapse_transitions
from app.tasks.broker_log import BrokerLogTail

U = "20363930594D50170004003A"
CONNECT = (f"2026-09-26T09:00:01: New client connected from 172.19.0.1:51672 as {U} "
           f"(p4, c1, k300, u'{U}').")


def test_connect_line():
    p = parse_line(CONNECT)
    assert p is not None
    assert (p.uuid, p.online, p.keepalive, p.ts) == (U, True, 300, "2026-09-26T09:00:01")


def test_disconnect_line_shapes():
    for line in (
        f"2026-09-26T09:07:31: Client {U} disconnected.",
        f"2026-09-26T09:07:31: Client {U} closed its connection.",
        f"2026-09-26T09:07:31: Client {U} has exceeded timeout, disconnecting.",
        f"2026-09-26T09:07:31: Socket error on client {U}, disconnecting.",
    ):
        p = parse_line(line)
        assert p is not None, line
        assert (p.uuid, p.online, p.keepalive) == (U, False, None)


def test_real_broker_lines_ipv6_and_k60():
    # 인프라 실측 2026-09-26 (go-auth 이미지, IPv6 loopback)
    p = parse_line(f"2026-09-26T09:08:39: New client connected from ::1:38142 as {U} "
                   f"(p2, c1, k60, u'{U}').")
    assert p is not None and (p.uuid, p.online, p.keepalive) == (U, True, 60)
    p = parse_line(f"2026-09-26T09:08:39: Client {U} disconnected.")
    assert p is not None and p.online is False


def test_not_authorised_is_not_offline():
    line = f"2026-09-26T09:08:39: Client {U} disconnected, not authorised."
    assert parse_line(line) is None                 # OFFLINE 아님
    assert not_authorised(line) == U                # 세기만
    assert not_authorised(f"Client {U} disconnected, not authorized.") == U
    assert not_authorised("Client iotlight-backend disconnected, not authorised.") is None
    assert not_authorised(f"Client {U} disconnected.") is None
    assert parse_chunk(line) == []


def test_without_timestamp_still_parses():
    p = parse_line(f"Client {U} disconnected.")
    assert p is not None and p.online is False and p.ts is None
    p = parse_line(f"New client connected from [::1]:5 as {U} (p3, c1, k60, u'{U}').")
    assert p is not None and p.online is True and p.keepalive == 60


def test_non_uuid_client_ids_ignored():
    assert parse_line("2026-09-26T09:00:01: New client connected from 172.19.0.5:4 as "
                      "iotlight-backend (p4, c1, k60, u'server').") is None
    assert parse_line("2026-09-26T09:00:01: Client iotlight-backend disconnected.") is None
    assert parse_line("2026-09-26T09:00:01: Client healthcheck-1 closed its connection.") is None
    assert parse_line(f"2026-09-26T09:00:01: Client {U.lower()} disconnected.") is None
    assert parse_line("2026-09-26T09:00:01: mosquitto version 2.0.15 starting") is None
    assert parse_line("2026-09-26T09:00:01: Received PUBLISH from " + U) is None
    assert parse_line("") is None


def test_parse_chunk_keeps_order_and_collapse_keeps_last():
    text = "\n".join([
        CONNECT,
        f"2026-09-26T09:00:02: Client {U} disconnected.",
        CONNECT,
        "2026-09-26T09:00:03: Client 00112233445566778899AABB has exceeded timeout, disconnecting.",
    ])
    parsed = parse_chunk(text)
    assert [(p.uuid[:4], p.online) for p in parsed] == [
        ("2036", True), ("2036", False), ("2036", True), ("0011", False)]
    assert collapse_transitions((p.uuid, p.online) for p in parsed) == {
        U: True, "00112233445566778899AABB": False}


def test_next_offset_truncation():
    assert next_offset(100, 50) == 50
    assert next_offset(50, 50) == 50
    assert next_offset(10, 50) == 0   # 파일이 줄었다 → 처음부터


def test_tail_reads_only_complete_lines_and_restarts_on_truncate(tmp_path):
    path = tmp_path / "mosquitto.log"
    path.write_bytes(b"old line\n")
    tail = BrokerLogTail(str(path))
    tail._offset = path.stat().st_size            # 기동: EOF 부터
    assert tail.read_new() == []

    with path.open("ab") as f:
        f.write((CONNECT + "\n").encode() + b"partial")
    lines = tail.read_new()
    assert lines == [CONNECT]                     # 개행 없는 꼬리는 넘긴다
    with path.open("ab") as f:
        f.write(b" line\n")
    assert tail.read_new() == ["partial line"]

    # truncate → 처음부터 다시
    path.write_bytes(f"Client {U} disconnected.\n".encode())
    lines = tail.read_new()
    assert lines == [f"Client {U} disconnected."]
    assert tail.file_found is True
    assert tail.enabled is True
    assert BrokerLogTail("").enabled is False
