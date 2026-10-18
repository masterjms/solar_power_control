"""OTA 규칙 — pkg 만들기 · 다운로드 주소 · Range · OTA_ACK 판정 · 시간 초과 (문제점 56번, 사양서 §24).

DB·MQTT 를 모르는 순수 함수라 시험이 직접 부른다(tests/test_ota.py).

흐름(§24.1): 서버 OTA_START → 단말 OTA_ACK START → 단말이 MQTT 를 끊고 HTTPS 로 pkg 를 3,840 바이트씩(Range)
받음 → 재부팅·맞바꾸기 → 새 펌웨어가 접속해 OTA_ACK DONE. 확정 전 실패면 이전 펌웨어가 접속해 ROLLBACK.
한 대의 끝 = DONE·ROLLBACK·오류 OTA_ACK(재접속만으로 끝이 아니다, §24.5).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
import secrets
from dataclasses import dataclass

#: 펌웨어 한도 160KB → pkg(base64) 최대 218,456 글자(§24.2).
FW_MAX = 160 * 1024
PKG_MAX = 218_456
#: 단말이 받는 주소 길이 상한(§24.3).
URL_MAX = 160
#: 한 번에 받는 글자 수 — 단말 기본과 같은 값을 명시해 보낸다(§24.3 "넣으면 3,840 권장").
CHUNK = 3840
#: 주소 유효 시간(처음 보낸 때부터). 받기 최대 15분 + 다시 보내기 여유.
URL_VALID = dt.timedelta(hours=2)
#: START 를 기다리는 시간과 보내는 횟수(§24.4 — 30초, 같은 job_id 로 3회까지).
START_WAIT = dt.timedelta(seconds=30)
START_ATTEMPTS = 3
#: START 뒤 결과를 기다리는 시간(받기 최대 15분 + 롤백 판정 최대 1시간, §24.4).
RESULT_WAIT = dt.timedelta(minutes=90)

#: 단말 OTA_ACK result.
START = "START"
DONE = "DONE"
ROLLBACK = "ROLLBACK"
#: 명령 직후 거부 — 시작 안 함.
REFUSED = frozenset({"BAD", "STATE", "BATT", "BUSY"})
#: 다시 접속한 뒤 실패 — 이전 펌웨어 그대로.
FAILED_LATER = frozenset({"HTTP", "SIZE", "HASH", "FLASH"})
#: 다시 접속한 뒤 오는 결과(성공·되돌림·실패).
LATER = FAILED_LATER | {DONE, ROLLBACK}
RESULTS = REFUSED | LATER | {START}

OPEN = ("waiting", "sending", "running")
NO_RESPONSE = "NO_RESPONSE"

#: 영문·숫자·/ - _ . : 만(§24.3 — 쉼표·공백·따옴표·\ 가 있으면 단말 BAD). 스킴의 ':' 는 앞에서 따로 본다.
_URL_RE = re.compile(r"^https?://[A-Za-z0-9./_-]+(:[0-9]{1,5})?[A-Za-z0-9./_-]*$")


class PackageInvalid(ValueError):
    pass


@dataclass(frozen=True)
class Pkg:
    data: str
    size: int
    sha256: str


def make_pkg(fw: bytes) -> Pkg:
    """펌웨어 bin → pkg(표준 base64, `=` 채움, 줄바꿈 없는 한 줄). sha256 은 pkg 글자 기준(§24.2).

    `sha256` 은 반드시 서버가 pkg 에서 계산한다 — 단말이 인증서를 검증하지 못하므로 이것이 위조를 막는다(§24.5)."""
    if not fw:
        raise PackageInvalid("빈 파일입니다")
    if len(fw) > FW_MAX:
        raise PackageInvalid(f"펌웨어가 {len(fw):,} 바이트입니다 — 단말 한도 {FW_MAX:,} 바이트(160KB)")
    if fw[:1] in (b"{", b"<") or fw[:4] == b"PK\x03\x04":
        raise PackageInvalid("펌웨어(.bin)가 아닌 것 같습니다 — 빌드 결과 SolarLTE.bin 을 올리세요")
    data = base64.b64encode(fw).decode("ascii")
    if len(data) > PKG_MAX or len(data) % 4:
        raise PackageInvalid("pkg 크기가 범위를 벗어났습니다")
    return Pkg(data=data, size=len(data), sha256=hashlib.sha256(data.encode("ascii")).hexdigest())


def new_token() -> str:
    """주소 마지막 칸 — 무작위 16바이트 = base64url 22자(A-Z a-z 0-9 - _). 추측할 수 없다."""
    return secrets.token_urlsafe(16)


def url_path(item_id: int, uuid: str, token: str) -> str:
    return f"/ota/{item_id}/{uuid}/{token}"


def url_for(base: str, item_id: int, uuid: str, token: str) -> str:
    """단말에 줄 주소. 형식이 단말 규칙(§24.3)에 안 맞으면 ValueError — 보내기 전에 막는다."""
    url = base.rstrip("/") + url_path(item_id, uuid, token)
    if len(url) > URL_MAX:
        raise ValueError(f"OTA 주소가 {len(url)}자 — 단말 한도 {URL_MAX}자")
    if not _URL_RE.match(url):
        raise ValueError("OTA 주소에 단말이 못 읽는 글자가 있다(영문·숫자·/ - _ . 만)")
    return url


def start_payload(*, job_id: int, url: str, size: int, sha256: str) -> dict[str, object]:
    """OTA_START(§24.3). 키 순서 고정. 개별 topic 만(그룹·전체로 오면 단말이 버린다)."""
    return {"type": "OTA_START", "job_id": job_id, "pkg_version": 1, "url": url,
            "size": size, "sha256": sha256.lower(), "chunk": CHUNK}


def url_valid(first_sent_at: dt.datetime | None, now: dt.datetime) -> bool:
    return first_sent_at is not None and now < first_sent_at + URL_VALID


def token_ok(want: str | None, got: str) -> bool:
    return bool(want) and secrets.compare_digest(want.encode(), got.encode())


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """`Range: bytes=a-b` → (a, b) 닫힌 구간. 헤더가 없으면 None(전체).
    여러 구간·형식 오류·범위 밖은 ValueError(→ 416). 끝이 크기를 넘으면 잘라 준다(RFC 9110)."""
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise ValueError("Range 형식")
    if not m.group(1):  # 뒤에서 N 바이트
        n = int(m.group(2))
        if n <= 0:
            raise ValueError("Range 범위")
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        raise ValueError("Range 범위")
    return start, min(end, size - 1)


def on_ack(status: str, result: str) -> str | None:
    """OTA_ACK 를 받은 항목의 새 상태. None = 무시(중복·늦게 온 거부 등).

    · START: 보낸 뒤(sending) → running. START 3회가 다 지나 응답 없음으로 닫혔어도 START 가 오면 되살린다.
    · 명령 직후 거부(BAD·STATE·BATT·BUSY): sending 일 때만 — running 중이면 다시 보낸 OTA_START 에 대한
      BUSY(같은 job_id)라 무시한다.
    · 다시 접속한 뒤 결과(DONE·ROLLBACK·HTTP·SIZE·HASH·FLASH): sending·running·응답 없음에서 받는다(늦게 와도 결과로).
      DONE·ROLLBACK 은 1분마다 다시 오므로 이미 그 결과면 무시."""
    if result not in RESULTS:
        return None
    if result == START:
        return "running" if status in ("sending", NO_RESPONSE) else None
    if result in REFUSED:
        return result if status == "sending" else None
    if status in ("sending", "running", NO_RESPONSE):
        return result
    return None


def timeout_step(status: str, attempts: int, sent_at: dt.datetime | None,
                 started_at: dt.datetime | None, now: dt.datetime) -> str | None:
    """진행기가 열린 항목에 할 일. "resend" / NO_RESPONSE / None."""
    if status == "sending" and sent_at is not None and now - sent_at >= START_WAIT:
        return "resend" if attempts < START_ATTEMPTS else NO_RESPONSE
    if status == "running" and started_at is not None and now - started_at >= RESULT_WAIT:
        return NO_RESPONSE
    return None


#: 화면·기록에 쓰는 설명(§24.3 표).
RESULT_TEXT = {
    "DONE": "성공 — 새 펌웨어 확정",
    "ROLLBACK": "실패 — 새 펌웨어가 확정 전에 문제, 이전 펌웨어로 동작 중",
    "BAD": "시작 안 함 — 명령 형식 오류",
    "STATE": "시작 안 함 — 단말이 운영(승인) 상태가 아님",
    "BATT": "시작 안 함 — 저전압",
    "BUSY": "시작 안 함 — 이미 OTA 중이거나 지난 결과를 아직 못 보냄",
    "HTTP": "실패 — 다운로드 실패, 이전 펌웨어 그대로",
    "SIZE": "실패 — 받은 크기가 다름, 이전 펌웨어 그대로",
    "HASH": "실패 — SHA-256 다름·파일 깨짐, 이전 펌웨어 그대로",
    "FLASH": "실패 — 단말 저장·적용 실패, 이전 펌웨어 그대로",
}
