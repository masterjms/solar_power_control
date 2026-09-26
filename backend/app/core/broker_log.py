"""Mosquitto 로그 파서 — 접속/끊김 줄에서 (UUID, online) 을 뽑는다 (ADR-004, 사양서 §16.1 A).

WD-N522S 모뎀은 LWT 를 못 넣는다. 브로커는 keepalive 로 끊김을 알지만 topic 으로 알려
주지 않으므로, `connection_messages true` 로 나오는 로그를 서버가 읽는다.

Mosquitto 2.0.15, `log_timestamp_format %Y-%m-%dT%H:%M:%S` 기준 줄 모양:

    2026-09-26T09:00:01: New client connected from 172.19.0.1:51672 as 20363930594D50170004003A
                         (p4, c1, k300, u'20363930594D50170004003A').      ← 실제로는 한 줄
    2026-09-26T09:07:31: Client 20363930594D50170004003A disconnected.
    2026-09-26T09:07:31: Client 20363930594D50170004003A closed its connection.
    2026-09-26T09:07:31: Client 20363930594D50170004003A has exceeded timeout, disconnecting.
    2026-09-26T09:07:31: Socket error on client 20363930594D50170004003A, disconnecting.

    2026-09-26T09:08:39: Client 20363930594D50170004003A disconnected, not authorised.

마지막 줄(인증 실패)은 **OFFLINE 이 아니다** — 접속된 적이 없다. 무시하고 not_authorised()
로만 센다(HMAC 키 전환 중 옛 펌웨어 단말을 세는 용도). 주소는 `172.19.0.1:51672` 도
`::1:38142` 도 온다(IPv6).

client id 가 UUID 형식(`^[0-9A-F]{24}$`)이 아닌 줄(백엔드 `iotlight-backend`, healthcheck,
시험 도구)은 무시한다. 타임스탬프가 없는 줄(stdout 형식이 다를 때)도 본문만 맞으면 받는다.

순수 함수. 파일·DB 는 tasks/broker_log.py 가 만진다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.constants import UUID_RE

_TS = r"(?:(?P<ts>\S+): )?"
_ID = r"(?P<cid>[^\s(),']+)"

_CONNECTED_RE = re.compile(
    _TS + r"New client connected from (?P<addr>\S+) as " + _ID
    + r" \((?P<opts>[^)]*)\)\.?\s*$"
)
_DISCONNECTED_RE = re.compile(
    _TS + r"Client " + _ID
    + r" (?:disconnected|closed its connection|has exceeded timeout, disconnecting)\.?\s*$"
)
_SOCKET_ERROR_RE = re.compile(_TS + r"Socket error on client " + _ID + r", disconnecting\.?\s*$")
_NOT_AUTHORISED_RE = re.compile(
    _TS + r"Client " + _ID + r" disconnected, not authori[sz]ed\.?\s*$"
)
_KEEPALIVE_RE = re.compile(r"\bk(?P<ka>\d+)\b")


@dataclass(frozen=True)
class PresenceLine:
    uuid: str
    online: bool
    #: 접속 줄의 keepalive(k300). 지금은 저장하지 않지만 파싱해 둔다(ka_device 대조용).
    keepalive: int | None = None
    #: 원문 타임스탬프 문자열(있으면). 시각 판정은 서버 수신 시각을 쓴다.
    ts: str | None = None


def parse_line(line: str) -> PresenceLine | None:
    """한 줄 → PresenceLine. 접속/끊김 줄이 아니거나 UUID 가 아니면 None."""
    line = line.rstrip("\r\n")
    m = _CONNECTED_RE.match(line)
    if m is not None:
        cid = m.group("cid")
        if not UUID_RE.match(cid):
            return None
        ka_match = _KEEPALIVE_RE.search(m.group("opts") or "")
        keepalive = int(ka_match.group("ka")) if ka_match else None
        return PresenceLine(uuid=cid, online=True, keepalive=keepalive, ts=m.group("ts"))
    m = _DISCONNECTED_RE.match(line) or _SOCKET_ERROR_RE.match(line)
    if m is not None:
        cid = m.group("cid")
        if not UUID_RE.match(cid):
            return None
        return PresenceLine(uuid=cid, online=False, ts=m.group("ts"))
    return None


def not_authorised(line: str) -> str | None:
    """인증 실패 끊김 줄이면 그 client id(UUID 형식일 때만). OFFLINE 으로 취급하지 않는다."""
    m = _NOT_AUTHORISED_RE.match(line.rstrip("\r\n"))
    if m is None:
        return None
    cid = m.group("cid")
    return cid if UUID_RE.match(cid) else None


def parse_chunk(text: str) -> list[PresenceLine]:
    """여러 줄 → 순서대로. 같은 UUID 가 여러 번 나오면 전부 돌려준다 — 마지막 것이 이기게
    접는 일은 호출부(apply)가 한다."""
    out: list[PresenceLine] = []
    for line in text.splitlines():
        parsed = parse_line(line)
        if parsed is not None:
            out.append(parsed)
    return out


def next_offset(size: int, last_offset: int) -> int:
    """파일 크기가 줄었으면(truncate/회전) 처음부터, 아니면 이어서."""
    return 0 if size < last_offset else last_offset
