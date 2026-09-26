"""Mosquitto 로그 tail — 접속/끊김을 device.online 으로 (ADR-004, 사양서 §16.1 방법 A).

모뎀이 LWT 를 못 넣어서 브로커 로그 파일(공유 볼륨 MOSQUITTO_LOG_PATH)을 읽는다.

    0.5초마다: 파일 크기 확인 → 새로 붙은 바이트를 읽어 줄 단위로 파싱(core/broker_log)
              → UUID 별 마지막 전이만 남겨 한 트랜잭션에 반영(core/presence.apply_presence)
    기동 시  : **파일 끝부터** 읽는다. 옛 줄을 되감아 반영하면 지금과 다른 상태를 쓰게 된다.
              그 사이 놓친 전이는 (1) 단말이 재접속하면 새 줄로 맞춰지고 (2) 수신 시각 보조
              규칙(presence C)이 오프라인 오판을 막는다.
    파일 없음: 5초마다 다시 찾는다(브로커가 아직 안 떴거나 볼륨 이름이 다름). /health 의
              broker_log_tail 이 false 로 남아 운영자가 안다.
    truncate/회전: 크기가 마지막 오프셋보다 작아지면 처음부터 읽는다. entrypoint 감시 루프가
              50 MB 에서 truncate + HUP 하므로 inode 는 그대로다(회전으로 파일이 바뀌면
              열린 핸들이 옛 파일을 보므로 poll 마다 다시 연다).
    줄 경계  : 마지막 줄이 아직 다 안 써졌을 수 있다 — 개행 없는 꼬리는 다음 poll 로 넘긴다.

MOSQUITTO_LOG_PATH 가 비어 있으면 태스크를 띄우지 않는다(개발 PC). 그때는 REGISTER 수신과
LWT 만이 online 을 바꾼다.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os

from app.config import settings
from app.core import presence
from app.core.broker_log import next_offset, not_authorised, parse_line
from app.core.metrics import metrics
from app.db import session_scope

log = logging.getLogger(__name__)

POLL_SEC = 0.5
MISSING_RETRY_SEC = 5.0
#: 한 poll 에 읽는 상한. 브로커 재시작 뒤 1만 줄이 한꺼번에 붙어도 몇 번에 나눠 처리한다.
_READ_MAX = 4 * 1024 * 1024


class BrokerLogTail:
    def __init__(self, path: str | None = None) -> None:
        self._path = settings.mosquitto_log_path if path is None else path
        self._offset = 0
        self._tail = b""
        self._task: asyncio.Task | None = None
        self._stopping = False
        #: 파일을 찾아 읽고 있는가. /health 의 broker_log_tail.
        self.file_found = False

    @property
    def enabled(self) -> bool:
        return bool(self._path)

    @property
    def alive(self) -> bool:
        """태스크가 살아 있고 파일을 읽고 있다."""
        return self._task is not None and not self._task.done() and self.file_found

    # ── 수명주기 ────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.enabled:
            log.info("MOSQUITTO_LOG_PATH 비어 있음 — 브로커 로그 tail 을 돌리지 않는다")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="broker-log-tail")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # 기동: 파일 끝에서 시작.
        while not self._stopping:
            try:
                self._offset = os.path.getsize(self._path)
                self.file_found = True
                log.info("브로커 로그 tail 시작: %s (offset %d)", self._path, self._offset)
                break
            except OSError:
                self.file_found = False
                await asyncio.sleep(MISSING_RETRY_SEC)

        while not self._stopping:
            try:
                await self.poll_once()
                await asyncio.sleep(POLL_SEC)
            except asyncio.CancelledError:
                raise
            except FileNotFoundError:
                if self.file_found:
                    log.warning("브로커 로그 파일이 사라짐: %s — 5초마다 재시도", self._path)
                self.file_found = False
                self._offset = 0
                self._tail = b""
                await asyncio.sleep(MISSING_RETRY_SEC)
            except Exception:  # noqa: BLE001 - 한 번 실패해도 루프는 계속 돈다
                metrics.broker_log_errors += 1
                log.exception("브로커 로그 tail 오류")
                await asyncio.sleep(MISSING_RETRY_SEC)

    # ── 1회 poll ────────────────────────────────────────────────────────
    def read_new(self) -> list[str]:
        """새로 붙은 줄들(개행 완료분). 파일 접근만 하고 DB 는 안 만진다(테스트가 직접 부른다)."""
        size = os.path.getsize(self._path)
        self.file_found = True
        start = next_offset(size, self._offset)
        if start != self._offset:
            log.info("브로커 로그가 줄어듦(truncate/회전) — 처음부터 다시 읽는다")
            self._tail = b""
        if size <= start:
            self._offset = start
            return []
        with open(self._path, "rb") as f:
            f.seek(start)
            chunk = f.read(min(size - start, _READ_MAX))
        self._offset = start + len(chunk)
        data = self._tail + chunk
        head, sep, rest = data.rpartition(b"\n")
        if not sep:
            self._tail = data
            return []
        self._tail = rest
        return head.decode("utf-8", errors="replace").splitlines()

    async def poll_once(self) -> tuple[int, int]:
        """새 줄을 읽어 반영. 반환 (ONLINE 전이 수, OFFLINE 전이 수)."""
        lines = self.read_new()
        if not lines:
            return 0, 0
        metrics.broker_log_lines += len(lines)
        parsed = [p for p in (parse_line(ln) for ln in lines) if p is not None]
        # 인증 실패(HMAC 키 불일치·옛 펌웨어)는 접속된 적이 없으니 OFFLINE 이 아니다. 세기만.
        rejected = [u for u in (not_authorised(ln) for ln in lines) if u]
        if rejected:
            metrics.broker_log_not_authorised += len(rejected)
            log.warning("브로커 인증 실패 단말 %d대 (예: %s)", len(rejected), rejected[0])
        transitions = presence.collapse_transitions((p.uuid, p.online) for p in parsed)
        if not transitions:
            return 0, 0
        now = dt.datetime.now(dt.timezone.utc)
        async with session_scope() as db:
            on, off = await presence.apply_presence(db, transitions, now, source="broker_log")
        metrics.broker_log_online += on
        metrics.broker_log_offline += off
        if on or off:
            log.info("브로커 로그: ONLINE %d, OFFLINE %d (줄 %d)", on, off, len(lines))
        return on, off
