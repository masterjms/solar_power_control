"""/health · /api/metrics · /api/me · /api/admin/*"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import ids
from app.core.auth import Principal, current_user
from app.core.metrics import metrics
from app.core.server_settings import runtime
from app.db import get_db
from app.modules.deps import get_broker_log, get_buffer, get_config_sync, get_connection
from app.modules.mqtt_auth.router import active_key_ids
from app.mqtt.config_sync import ConfigSyncQueue
from app.mqtt.connection import MqttConnection
from app.mqtt.telemetry_buffer import TelemetryBuffer
from app.tasks import daily_rollup, partitions
from app.tasks.broker_log import BrokerLogTail

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(
    db: AsyncSession = Depends(get_db),
    conn: MqttConnection = Depends(get_connection),
    buffer: TelemetryBuffer = Depends(get_buffer),
    config_sync: ConfigSyncQueue = Depends(get_config_sync),
    broker_log: BrokerLogTail = Depends(get_broker_log),
) -> dict[str, Any]:
    """컨테이너 healthcheck 가 부른다. DB 가 죽어도 200 을 주되 db_ok=false 로 표시한다 —
    DB 장애 때 백엔드까지 재시작되면 버퍼에 남은 것과 MQTT 세션을 같이 잃는다."""
    db_ok = True
    try:
        # DB 장애 때 연결·풀 대기로 /health 가 매달리지 않게 2초에서 끊는다 — 감시는 "빨리 db_ok=false" 가 필요하다.
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=2.0)
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "ok": conn.is_connected and db_ok,
        "mqtt_connected": conn.is_connected,
        "db_ok": db_ok,
        "buffer_pending": buffer.pending_count,
        "register_queue": config_sync.pending_count,
        "telemetry_dropped": metrics.telemetry_dropped,
        "flush_failures": metrics.flush_failures,
        #: 태스크가 살아 있고 로그 파일을 찾았다. 경로가 비어 있으면(개발 PC) false.
        "broker_log_tail": broker_log.alive,
        #: 활성 HMAC 키 ID 만(값은 절대 안 나간다).
        "hmac_keys": active_key_ids(),
        "test_account_enabled": settings.mqtt_test_account_enabled,
        "env": settings.app_env,
    }


@router.get("/api/ui-config")
async def ui_config() -> dict[str, Any]:
    """화면 설정. 카카오 **JavaScript** 키(지도)는 브라우저가 쓰는 공개 키라 여기서 준다 —
    REST 키(kakao_rest_api_key)는 절대 싣지 않는다."""
    return {
        "kakao_js_key": settings.kakao_js_key or None,
        "ghg_kg_per_kwh": runtime.ghg_kg_per_kwh,
        #: 원격 명령 응답 대기(서버 설정, ADR-012) — 화면 안내 문구가 지금 값을 따라간다.
        "command_wait_sec": runtime.command_wait_sec,
        "command_attempts": runtime.command_attempts,
    }


@router.get("/api/metrics")
async def get_metrics(
    db: AsyncSession = Depends(get_db),
    conn: MqttConnection = Depends(get_connection),
    buffer: TelemetryBuffer = Depends(get_buffer),
    config_sync: ConfigSyncQueue = Depends(get_config_sync),
    broker_log: BrokerLogTail = Depends(get_broker_log),
) -> dict[str, Any]:
    snapshot = metrics.snapshot(
        buffer_pending=buffer.pending_count,
        register_queue=config_sync.pending_count,
        mqtt_connected=conn.is_connected,
        broker_log_tail=broker_log.alive,
    )
    try:
        current = await ids.current_cmd_seq(db)
    except Exception:  # noqa: BLE001
        current = None
    snapshot["cmd_seq"] = current
    # uint32 상한 근접 경보 (docs/03 command.seq).
    snapshot["cmd_seq_alert"] = current is not None and current >= ids.SEQ_ALERT_THRESHOLD
    return snapshot


@router.get("/api/me")
async def me(principal: Principal = Depends(current_user)) -> dict[str, str]:
    """현재 사용자·역할(ADR-005). 화면이 최고관리자 전용 버튼(전체 명령·트리 편집)을 숨긴다."""
    return {"user": principal.user, "role": principal.role}


@router.post("/api/admin/rollup")
async def trigger_rollup(
    day: dt.date | None = Query(default=None, description="KST 날짜 YYYY-MM-DD. 생략하면 어제"),
) -> dict[str, Any]:
    """일 집계 수동 실행. 같은 날을 다시 돌리면 덮어쓴다."""
    target = day or daily_rollup.yesterday_kst()
    count = await daily_rollup.run(target)
    return {"day": target.isoformat(), "devices": count}


@router.post("/api/admin/partitions")
async def trigger_partitions() -> dict[str, Any]:
    """파티션 점검 수동 실행 (이번 달·다음 달 생성, 보존 초과 DROP)."""
    await partitions.run()
    return {"ok": True}
