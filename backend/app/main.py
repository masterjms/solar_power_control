"""앱 조립.

여기서 하는 일은 세 가지뿐이다 — 수명주기 관리, 라우터 등록, 미들웨어 등록.
비즈니스 로직은 한 줄도 두지 않는다.

한 프로세스 안에서 같이 돈다(단일 프로세스, ADR-001):
  · REST API          요청/응답
  · MQTT 수신         브로커 구독 → 버퍼 → DB
  · TelemetryBuffer   1초 배치 flush
  · ConfigSyncQueue   CONFIG_SET 발행(토큰 버킷)
  · 스케줄러          파티션 점검(매일), 일 집계(00:30 KST)

기동 순서가 중요하다: 파티션 확인 → 계정 내보내기 → 버퍼 warm → MQTT 연결.
파티션이 없으면 첫 flush 가 실패하고, 계정이 브로커에 없으면 단말이 못 붙는다.
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
from app.db import SessionFactory, engine, session_scope
from app.errors import register_exception_handlers
from app.modules.device import service as device_service
from app.modules.device.router import router as device_router
from app.modules.system.router import router as system_router
from app.mqtt.config_sync import ConfigSyncQueue
from app.mqtt.connection import MqttConnection
from app.mqtt.handlers import Dispatcher
from app.mqtt.publisher import MqttPublisher
from app.mqtt.telemetry_buffer import TelemetryBuffer
from app.tasks import daily_rollup, partitions

# ⚠ Windows 에서 `python -m uvicorn app.main:app` 로 띄우면 MQTT 가 죽는다.
#   uvicorn 이 ProactorEventLoop 를 강제하는데 paho 가 쓰는 add_reader 가 거기 없다.
#   개발 서버는 backend/run.py 로 띄운다(셀렉터 루프를 직접 만든다). 컨테이너는 Linux.

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ── 기동 ────────────────────────────────────────────────────────────
    if settings.app_host == "0.0.0.0":  # noqa: S104
        log.warning("APP_HOST=0.0.0.0 — 2차 REST 는 인증이 없다. 방화벽/compose 로 막았는지 확인")
    await partitions.run()

    connection = MqttConnection(on_message=lambda t, raw: dispatcher(t, raw))
    publisher = MqttPublisher(connection)
    config_sync = ConfigSyncQueue(publisher)
    telemetry_buffer = TelemetryBuffer(config_sync=config_sync)
    dispatcher = Dispatcher(buffer=telemetry_buffer, config_sync=config_sync)

    app.state.mqtt = connection
    app.state.publisher = publisher
    app.state.config_sync = config_sync
    app.state.telemetry_buffer = telemetry_buffer

    # 단말별 MQTT 계정을 기동 때마다 다시 내보낸다 — DB 복구·볼륨 재생성 뒤에도
    # 브로커 passwd 가 DB(정본)와 같아진다. 실패해도 기동은 계속한다.
    try:
        async with session_scope() as db:
            await device_service.export_broker_accounts(db)
    except Exception:  # noqa: BLE001
        log.exception("기동 시 MQTT 계정 내보내기 실패 (다음 import 때 재시도)")

    try:
        async with SessionFactory() as db:
            warmed = await telemetry_buffer.warm(db)
        log.info("last_sq 캐시 적재: %d대", warmed)
    except Exception:  # noqa: BLE001
        log.exception("last_sq 캐시 적재 실패 — 첫 TM 의 유실/재부팅 판정을 건너뛴다")

    await config_sync.start()
    await telemetry_buffer.start()
    await connection.start()

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
    scheduler.start()
    app.state.scheduler = scheduler

    log.info("기동 완료 (env=%s, root=%s)", settings.app_env, settings.mqtt_topic_root)
    yield

    # ── 종료 ────────────────────────────────────────────────────────────
    scheduler.shutdown(wait=False)
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
app.include_router(device_router)
