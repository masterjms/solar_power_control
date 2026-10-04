"""/api/system/status — 서버 상태 화면(문제점 31번). 최고관리자만(access_guard SUPER_ONLY).

단말이 아니라 **서버 자체**가 괜찮은지 — 장애가 났을 때 "단말 문제인지 서버 문제인지" 가르는 화면.
운영자 말로 다섯 묶음: 브로커 / DB·디스크 / 처리 / 보안 / 서버. 원문(/health·/api/metrics)은 화면이 "자세히"로 접는다.

디스크는 backend 컨테이너의 `/` 를 잰다 — 컨테이너 파일 시스템은 호스트 루트 디스크(EBS) 위에 있으므로 같은 값이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import shutil
import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import presence
from app.core.auth import Principal, current_user, require_super
from app.core.metrics import metrics
from app.core.server_settings import runtime
from app.db import get_db
from app.models.command import Command
from app.models.device import Device
from app.models.schedule import DeployItem
from app.models.system import AdminUser, LoginLog
from app.modules.deps import get_broker_log, get_buffer, get_config_sync, get_connection
from app.modules.mqtt_auth.router import active_key_ids
from app.mqtt.config_sync import ConfigSyncQueue
from app.mqtt.connection import MqttConnection
from app.mqtt.telemetry_buffer import TelemetryBuffer
from app.tasks.broker_log import BrokerLogTail

router = APIRouter(tags=["system"])

DISK_WARN_PCT = 80


async def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """브로커 리스너가 실제로 듣는지 — TCP 연결만 해 보고 닫는다."""
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    w.close()
    with contextlib.suppress(OSError):
        await w.wait_closed()
    return True


def _iso(ts: float | None) -> str | None:
    return None if ts is None else dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


@router.get("/api/system/status")
async def status(
    me: Principal = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    conn: MqttConnection = Depends(get_connection),
    buffer: TelemetryBuffer = Depends(get_buffer),
    config_sync: ConfigSyncQueue = Depends(get_config_sync),
    broker_log: BrokerLogTail = Depends(get_broker_log),
) -> dict[str, Any]:
    require_super(me, action="system.status")
    now = dt.datetime.now(dt.timezone.utc)

    # ── 브로커 ──
    host = settings.mqtt_host
    p1883, p8883 = await asyncio.gather(_port_open(host, 1883), _port_open(host, 8883))
    online = int(await db.scalar(select(func.count()).select_from(Device)
                                 .where(presence.online_clause(now))) or 0)
    broker_conn = int(await db.scalar(select(func.count()).select_from(Device)
                                      .where(Device.online.is_(True))) or 0)
    active = int(await db.scalar(select(func.count()).select_from(Device)
                                 .where(Device.state == "ACTIVE")) or 0)

    # ── DB·디스크 ──
    db_ok, db_bytes, tables = True, None, []
    try:
        db_bytes = int(await asyncio.wait_for(
            db.scalar(text("SELECT pg_database_size(current_database())")), timeout=3.0) or 0)
        rows = await db.execute(text(
            "SELECT relname, pg_total_relation_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relispartition "
            "ORDER BY 2 DESC LIMIT 8"))
        tables = [{"name": n, "bytes": int(b)} for n, b in rows]
        # 10분 보고 원문(telemetry)은 월 파티션 표 — 부모는 0 이라 자식 합으로 따로 넣는다.
        tele = await db.scalar(text(
            "SELECT coalesce(sum(pg_total_relation_size(inhrelid)),0) FROM pg_inherits "
            "WHERE inhparent = 'telemetry'::regclass"))
        tables.append({"name": "telemetry", "bytes": int(tele or 0)})
        tables = sorted(tables, key=lambda t: -t["bytes"])[:6]
    except Exception:  # noqa: BLE001
        db_ok = False
    du = shutil.disk_usage("/")
    disk_pct = round(du.used / du.total * 100, 1) if du.total else None

    # ── 처리 ──
    open_cmds = int(await db.scalar(select(func.count()).select_from(Command)
                                    .where(Command.finished_at.is_(None))) or 0)
    open_deploy = int(await db.scalar(select(func.count()).select_from(DeployItem)
                                      .where(DeployItem.status.in_(("waiting", "reading", "sent"))))
                      or 0)

    # ── 보안 ──
    fails = int(await db.scalar(select(func.count()).select_from(LoginLog).where(
        LoginLog.ok.is_(False), LoginLog.at >= now - dt.timedelta(hours=24))) or 0)
    expiring = int(await db.scalar(select(func.count()).select_from(AdminUser).where(
        AdminUser.expires_at.is_not(None), AdminUser.expires_at > now,
        AdminUser.expires_at <= now + dt.timedelta(days=7))) or 0)

    up = time.time() - metrics.started_at
    return {
        "at": now.isoformat(),
        "broker": {
            "backend_connected": conn.is_connected, "port_1883": p1883, "port_8883": p8883,
            "devices_online": online, "devices_broker_connected": broker_conn,
            "devices_active": active, "log_tail": broker_log.alive,
        },
        "db": {
            "ok": db_ok, "size_bytes": db_bytes, "tables": tables,
            "disk_total_bytes": du.total, "disk_free_bytes": du.free, "disk_used_pct": disk_pct,
            "disk_warn": disk_pct is not None and disk_pct >= DISK_WARN_PCT,
            "last_rollup_at": _iso(metrics.last_rollup_at), "last_rollup_day": metrics.last_rollup_day,
            "last_purge_at": _iso(metrics.last_purge_at),
            "telemetry_months": runtime.telemetry_retention_months,
        },
        "processing": {
            "buffer_pending": buffer.pending_count, "register_queue": config_sync.pending_count,
            "telemetry_dropped": metrics.telemetry_dropped, "flush_failures": metrics.flush_failures,
            "commands_open": open_cmds, "deploy_open": open_deploy,
            "mqtt_reconnects": metrics.mqtt_reconnects,
        },
        "security": {
            "hmac_keys": active_key_ids(), "test_account_enabled": settings.mqtt_test_account_enabled,
            "login_failures_24h": fails, "accounts_expiring_7d": expiring,
        },
        "server": {
            "version": os.environ.get("GIT_SHA") or "unknown",
            "started_at": _iso(metrics.started_at), "uptime_sec": int(up), "env": settings.app_env,
        },
    }
