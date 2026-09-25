"""MQTT 연결 관리.

브로커 연결 1개를 소유하고, 끊기면 1→30초 백오프로 다시 붙는다. 재연결 때마다 구독을
다시 건다(Clean Session 이라 브로커가 기억하지 않는다). 수신 메시지는 콜백으로 흘려보낸다.

⚠ raw_publish() 를 직접 호출하지 않는다. 발행은 전부 publisher.MqttPublisher 를 거친다
   (payload 크기 · QoS/retain 정책이 거기 걸려 있다).

4차 TLS 전환은 settings.mqtt_tls 와 포트만 바뀐다 — 이 파일 밖은 손대지 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import Awaitable, Callable

import aiomqtt

from app.config import settings
from app.core.metrics import metrics
from app.errors import MqttUnavailable
from app.mqtt.topics import SUBSCRIPTIONS

log = logging.getLogger(__name__)

#: (topic, payload) 를 받는 수신 콜백.
MessageHandler = Callable[[str, bytes], Awaitable[None]]

_RECONNECT_MIN_SEC = 1.0
_RECONNECT_MAX_SEC = 30.0


class MqttConnection:
    """브로커 연결의 수명을 관리한다.

    연결이 끊긴 동안 raw_publish 는 MqttUnavailable 을 던진다 — 조용히 삼키면
    "명령을 보냈는데 아무 일도 안 일어남"이 되어 최악이다.
    """

    def __init__(self, on_message: MessageHandler) -> None:
        self._on_message = on_message
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self._sessions = 0

    # ── 수명주기 ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_forever(), name="mqtt-connection")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._client = None
        self._connected.clear()

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    async def wait_connected(self, timeout: float) -> bool:
        """연결을 전제로 하는 기동 작업이 기다릴 때 쓴다."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ── 발행 (publisher 전용) ───────────────────────────────────────────
    async def raw_publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
        client = self._client
        if client is None:
            raise MqttUnavailable()
        await client.publish(topic, payload=payload, qos=qos, retain=retain)

    # ── 내부 ────────────────────────────────────────────────────────────
    def _tls_context(self) -> ssl.SSLContext | None:
        if not settings.mqtt_tls:
            return None
        return ssl.create_default_context()

    async def _run_forever(self) -> None:
        delay = _RECONNECT_MIN_SEC
        while not self._stopping.is_set():
            try:
                await self._session()
                delay = _RECONNECT_MIN_SEC  # 정상 종료였다면 백오프를 되돌린다
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 어떤 예외든 재연결로 흡수한다
                self._client = None
                self._connected.clear()
                log.warning("MQTT 연결 끊김 (%s초 후 재시도): %s", delay, exc)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    return  # stop() 이 걸렸다
                except asyncio.TimeoutError:
                    delay = min(delay * 2, _RECONNECT_MAX_SEC)

    async def _session(self) -> None:
        """연결 1회분. 끊기면 예외가 나가고 _run_forever 가 다시 부른다."""
        async with aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_username or None,
            password=settings.mqtt_password or None,
            identifier=settings.mqtt_client_id,
            tls_context=self._tls_context(),
            keepalive=60,
        ) as client:
            self._client = client
            self._connected.set()
            self._sessions += 1
            if self._sessions > 1:
                metrics.mqtt_reconnects += 1
            log.info("MQTT 연결됨 %s:%s (tls=%s)", settings.mqtt_host, settings.mqtt_port,
                     settings.mqtt_tls)

            for topic, qos in SUBSCRIPTIONS:
                await client.subscribe(topic, qos=qos)
                log.info("MQTT 구독 %s (QoS %s)", topic, qos)

            async for message in client.messages:
                payload = message.payload
                if not isinstance(payload, bytes):
                    payload = bytes(payload or b"")
                try:
                    await self._on_message(str(message.topic), payload)
                except Exception:  # noqa: BLE001
                    # 메시지 1건의 처리 실패가 구독 루프를 끊게 두지 않는다.
                    log.exception("MQTT 메시지 처리 실패: %s", message.topic)
