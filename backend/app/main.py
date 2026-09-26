"""앱 조립.

여기서 하는 일은 세 가지뿐이다 — 수명주기 관리, 라우터 등록, 미들웨어 등록.
비즈니스 로직은 한 줄도 두지 않는다.

한 프로세스 안에서 같이 돈다(단일 프로세스, ADR-001):
  · REST API          요청/응답
  · MQTT 수신         브로커 구독 → 버퍼 → DB
  · TelemetryBuffer   1초 배치 flush
  · ConfigSyncQueue   REGISTER_ACK · CONFIG_SET 발행(토큰 버킷)
  · BrokerLogTail     Mosquitto 로그 → online/offline (ADR-004)
  · /internal/mqtt/*  브로커 인증 플러그인(go-auth)이 부르는 HMAC 인증·ACL (ADR-003)
  · 스케줄러          파티션 점검(매일), 일 집계(00:30 KST), 계정 파일 재조정(5분)

기동 순서가 중요하다: HMAC 키 검사 → 파티션 확인 → 계정 내보내기 → 버퍼 warm → MQTT 연결.
키가 틀리면 전 단말이 못 붙으므로 기동 때 죽는 편이 낫고, 파티션이 없으면 첫 flush 가
실패하고, server 계정이 브로커에 없으면 백엔드가 못 붙는다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app.config import settings
from app.core import device_password
from app.db import SessionFactory, engine, session_scope
from app.errors import register_exception_handlers
from app.modules.device import service as device_service
from app.modules.device.router import router as device_router
from app.modules.mqtt_auth.router import router as mqtt_auth_router
from app.modules.profile.router import router as profile_router
from app.modules.system.router import router as system_router
from app.mqtt.config_sync import ConfigSyncQueue
from app.mqtt.connection import MqttConnection
from app.mqtt.handlers import Dispatcher
from app.mqtt.publisher import MqttPublisher
from app.mqtt.telemetry_buffer import TelemetryBuffer
from app.tasks import daily_rollup, partitions
from app.tasks.broker_log import BrokerLogTail

# ⚠ Windows 에서 `python -m uvicorn app.main:app` 로 띄우면 MQTT 가 죽는다.
#   uvicorn 이 ProactorEventLoop 를 강제하는데 paho 가 쓰는 add_reader 가 거기 없다.
#   개발 서버는 backend/run.py 로 띄운다(셀렉터 루프를 직접 만든다). 컨테이너는 Linux.

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("app")


async def _wait_for_db(timeout_sec: float = 120.0) -> None:
    """DB 가 뜰 때까지 기다린다. compose 순서·RDS 재시작 어느 쪽이든 서버가 먼저 뜰 수 있다.
    시간 안에 안 뜨면 그래도 기동한다 — 수신 경로는 실패를 카운트하며 버티고, /health 가 알린다."""
    import asyncio
    import time

    from sqlalchemy import text

    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as e:  # noqa: BLE001
            if time.monotonic() > deadline:
                log.error("DB 대기 시간 초과 — DB 없이 기동한다: %s", e)
                return
            log.warning("DB 대기 중... (%s)", e.__class__.__name__)
            await asyncio.sleep(2)


async def _reconcile_accounts() -> None:
    try:
        async with session_scope() as db:
            await device_service.export_broker_accounts(db)
    except Exception:  # noqa: BLE001
        log.exception("MQTT 계정 재조정 실패 (5분 뒤 재시도)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ── 기동 ────────────────────────────────────────────────────────────
    if settings.app_host == "0.0.0.0":  # noqa: S104
        log.warning("APP_HOST=0.0.0.0 — REST 에 아직 인증이 없다. 방화벽/compose 로 막았는지 확인")
    # HMAC 키는 기동 때 검사한다. 틀린 채로 떠 있으면 단말 인증이 전부 403 이라 아무도 못 붙는다.
    hmac_keys = device_password.parse_keys(settings.mqtt_hmac_keys)
    if not hmac_keys:
        log.error("MQTT_HMAC_KEYS 가 비어 있다 — 단말 인증 전부 거부됨")
    elif "TEST" in hmac_keys and settings.is_prod:
        log.warning("운영(prod)인데 공개 시험 키(TEST)가 활성 목록에 있다 — K1 로 바꿀 것")
    else:
        log.info("HMAC 활성 키: %s", list(hmac_keys))
    await _wait_for_db()
    await partitions.run()

    connection = MqttConnection(on_message=lambda t, raw: dispatcher(t, raw))
    publisher = MqttPublisher(connection)
    config_sync = ConfigSyncQueue(publisher)
    telemetry_buffer = TelemetryBuffer(config_sync=config_sync)
    dispatcher = Dispatcher(buffer=telemetry_buffer, config_sync=config_sync)
    broker_log = BrokerLogTail()

    app.state.mqtt = connection
    app.state.publisher = publisher
    app.state.config_sync = config_sync
    app.state.telemetry_buffer = telemetry_buffer
    app.state.broker_log = broker_log

    # server(+시험) 계정 passwd/aclfile 을 기동 때마다 다시 내보낸다 — 볼륨 재생성·비밀번호
    # 변경 뒤에도 브로커 파일이 .env 와 같아진다. 실패해도 기동은 계속한다(5분 재조정).
    try:
        async with session_scope() as db:
            await device_service.export_broker_accounts(db)
    except Exception:  # noqa: BLE001
        log.exception("기동 시 MQTT 계정 파일 내보내기 실패 (5분 뒤 재시도)")

    try:
        async with SessionFactory() as db:
            warmed = await telemetry_buffer.warm(db)
        log.info("last_sq 캐시 적재: %d대", warmed)
    except Exception:  # noqa: BLE001
        log.exception("last_sq 캐시 적재 실패 — 첫 TM 의 유실/재부팅 판정을 건너뛴다")

    await config_sync.start()
    await telemetry_buffer.start()
    await connection.start()
    await broker_log.start()

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        partitions.run, "cron", hour=1, minute=0, id="partitions",
        coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        daily_rollup.run, "cron",
        hour=settings.rollup_hour_kst, minute=settings.rollup_minute_kst,
        id="daily-rollup", coalesce=True, max_instances=1,
        # 서버가 잠깐 멈췄다 살아나도 밀린 실행이 한꺼번에 터지지 않게 한다.
        misfire_grace_time=3600,
    )
    # 계정 파일 재조정: 기동 시 내보내기가 실패했거나 볼륨이 갈렸을 때 다시 맞춘다.
    # 해시 salt 가 결정적이라 내용이 같으면 md5 도 같고, entrypoint 감시 루프가 걸러
    # 브로커를 재시작하지 않는다(go-auth 는 HUP 으로 passwd 를 다시 읽지 못한다).
    scheduler.add_job(
        _reconcile_accounts, "interval", minutes=5, id="accounts-reconcile",
        coalesce=True, max_instances=1,
    )
    scheduler.start()
    app.state.scheduler = scheduler

    log.info("기동 완료 (env=%s, root=%s)", settings.app_env, settings.mqtt_topic_root)
    yield

    # ── 종료 ────────────────────────────────────────────────────────────
    scheduler.shutdown(wait=False)
    await broker_log.stop()
    # 연결을 먼저 끊어 새 메시지가 더 안 들어오게 한 뒤 버퍼를 마지막으로 비운다.
    await connection.stop()
    await telemetry_buffer.stop()
    await config_sync.stop()
    await engine.dispose()
    log.info("종료 완료")


app = FastAPI(
    title="iotlight 관제 서버",
    version="0.1.0",
    description="태양광 가로등 단말(STM32 + WD-N522S)을 MQTT 로 수집·제어하는 서버 API",
    lifespan=lifespan,
)


@app.middleware("http")
async def db_session_middleware(request: Request, call_next) -> Response:
    """요청 하나 = 트랜잭션 하나. 커밋을 응답 전송 전에 한다(app/db.py 참고).
    4xx·5xx 로 끝난 요청은 롤백한다 — 실패한 요청이 절반만 남으면 안 된다."""
    async with SessionFactory() as session:
        request.state.db = session
        try:
            response = await call_next(request)
        except Exception:
            await session.rollback()
            raise
        if response.status_code >= 400:
            await session.rollback()
        else:
            await session.commit()
        return response


register_exception_handlers(app)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(system_router)
app.include_router(mqtt_auth_router)
app.include_router(profile_router)
app.include_router(device_router)
