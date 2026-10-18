"""OTA 규칙(문제점 56번, 사양서 §24) — pkg·주소·Range·OTA_ACK 판정·시간 초과·진행 시간대."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib

import pytest

from app.core import ota_rules as orl
from app.core.server_settings import ota_window_open
from app.mqtt.publisher import encode

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 18, 2, 0, tzinfo=UTC)  # KST 11:00
UUID = "20363930594D50170004003A"


def test_make_pkg_is_base64_one_line_and_sha_of_text() -> None:
    fw = bytes(range(256)) * 10 + b"\x00\x00"
    pkg = orl.make_pkg(fw)
    assert "\n" not in pkg.data and pkg.size == len(pkg.data) and pkg.size % 4 == 0
    assert base64.b64decode(pkg.data) == fw
    # sha256 은 bin 이 아니라 pkg 글자(§24.2 sha256sum SolarLTE.b64)
    assert pkg.sha256 == hashlib.sha256(pkg.data.encode()).hexdigest()


def test_make_pkg_limits() -> None:
    assert orl.make_pkg(b"\x01" * orl.FW_MAX).size == orl.PKG_MAX  # 160KB → 218,456 (§24.2)
    for bad in (b"", b"\x01" * (orl.FW_MAX + 1), b'{"json": 1}', b"PK\x03\x04zip"):
        with pytest.raises(orl.PackageInvalid):
            orl.make_pkg(bad)


def test_url_rules() -> None:
    tok = orl.new_token()
    assert len(tok) == 22 and all(c.isalnum() or c in "-_" for c in tok)
    url = orl.url_for("https://enerz.co.kr/", 9001, UUID, tok)
    assert url == f"https://enerz.co.kr/ota/9001/{UUID}/{tok}" and len(url) <= orl.URL_MAX
    assert len(url) < 90  # 시트 56번 "약 80자"
    assert orl.url_for("http://192.168.0.10:8088", 1, UUID, tok).startswith("http://192.168.0.10:8088/ota/1/")
    with pytest.raises(ValueError):
        orl.url_for("https://enerz.co.kr/a b", 1, UUID, tok)  # 공백 → 단말 BAD
    with pytest.raises(ValueError):
        orl.url_for("https://" + "a" * 150 + ".kr", 1, UUID, tok)  # 160자 초과


def test_start_payload_no_escaped_slash_and_order() -> None:
    p = orl.start_payload(job_id=7, url=f"https://enerz.co.kr/ota/7/{UUID}/abc", size=181816,
                          sha256="AB" * 32)
    raw = encode(p).decode()
    assert "\\/" not in raw  # JSON 에서 / 를 \/ 로 이스케이프하지 않는다(§24.3)
    assert list(p) == ["type", "job_id", "pkg_version", "url", "size", "sha256", "chunk"]
    assert p["sha256"] == "ab" * 32 and p["chunk"] == 3840 and p["pkg_version"] == 1


def test_url_valid_two_hours() -> None:
    assert orl.url_valid(T0, T0 + dt.timedelta(minutes=119))
    assert not orl.url_valid(T0, T0 + dt.timedelta(hours=2))
    assert not orl.url_valid(None, T0)
    assert orl.token_ok("abc", "abc") and not orl.token_ok("abc", "abd") and not orl.token_ok(None, "")


def test_parse_range() -> None:
    assert orl.parse_range(None, 100) is None
    assert orl.parse_range("bytes=0-3839", 10000) == (0, 3839)
    assert orl.parse_range("bytes=7680-11519", 10000) == (7680, 9999)  # 마지막 조각은 잘라 준다
    assert orl.parse_range("bytes=9990-", 10000) == (9990, 9999)
    assert orl.parse_range("bytes=-10", 10000) == (9990, 9999)
    for bad in ("bytes=10000-", "bytes=5-1", "bytes=0-1,5-6", "items=0-1", "bytes=-"):
        with pytest.raises(ValueError):
            orl.parse_range(bad, 10000)


@pytest.mark.parametrize(("status", "result", "want"), [
    ("sending", "START", "running"),
    ("running", "START", None),            # 중복
    ("NO_RESPONSE", "START", "running"),   # 늦은 START 로 되살림
    ("sending", "BATT", "BATT"),
    ("running", "BUSY", None),             # 다시 보낸 OTA_START 에 대한 BUSY(같은 job_id)
    ("running", "DONE", "DONE"),
    ("running", "ROLLBACK", "ROLLBACK"),
    ("running", "HASH", "HASH"),
    ("sending", "HTTP", "HTTP"),           # START 를 놓쳤어도 결과는 받는다
    ("NO_RESPONSE", "DONE", "DONE"),       # 늦게 와도 결과로
    ("DONE", "DONE", None),                # 1분마다 다시 오는 DONE
    ("CANCELLED", "DONE", None),
    ("running", "WHAT", None),
])
def test_on_ack(status: str, result: str, want: str | None) -> None:
    assert orl.on_ack(status, result) == want


def test_timeout_step() -> None:
    s = T0
    assert orl.timeout_step("sending", 1, s, None, s + dt.timedelta(seconds=29)) is None
    assert orl.timeout_step("sending", 1, s, None, s + dt.timedelta(seconds=30)) == "resend"
    assert orl.timeout_step("sending", 3, s, None, s + dt.timedelta(seconds=30)) == orl.NO_RESPONSE
    assert orl.timeout_step("running", 1, s, s, s + dt.timedelta(minutes=89)) is None
    # 15분이면 롤백 판정 중인 단말을 실패로 본다 — 1시간 30분(단말측 결론 2)
    assert orl.timeout_step("running", 1, s, s, s + dt.timedelta(minutes=90)) == orl.NO_RESPONSE
    assert orl.timeout_step("waiting", 0, None, None, s) is None


def test_window() -> None:
    kst = lambda h: dt.datetime(2026, 10, 18, h, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))  # noqa: E731
    assert ota_window_open(kst(10), 10, 16) and ota_window_open(kst(15), 10, 16)
    assert not ota_window_open(kst(16), 10, 16) and not ota_window_open(kst(9), 10, 16)
    assert ota_window_open(kst(23), 22, 4) and ota_window_open(kst(3), 22, 4) and not ota_window_open(kst(12), 22, 4)
    assert ota_window_open(kst(3), 0, 0)  # 같으면 하루 종일
