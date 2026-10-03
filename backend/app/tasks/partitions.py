"""telemetry 월 파티션 관리 + device_event 보존 삭제.

파티션 경계는 **UTC 월** 이다. received_at 이 timestamptz 라 어느 시간대 경계든 상관없지만,
KST 로 자르면 파티션 이름(YYYYMM)과 DDL 의 경계 문자열이 어긋나 보여 혼란스럽다.
일 집계(daily_rollup)는 KST 날짜로 자르지만 그건 조회 범위지 파티션 경계가 아니다.

기동 시 + 매일 1회:
  1. 이번 달·다음 달 파티션이 없으면 만든다 (다음 달을 미리 만들어야 월 바뀌는 자정에
     INSERT 가 "no partition of relation" 으로 실패하지 않는다)
  2. TELEMETRY_RETENTION_MONTHS 보다 오래된 파티션은 DROP (DELETE 가 아니라 DROP —
     수천만 행을 지우는 DELETE 는 몇 시간짜리 vacuum 을 남긴다)
  3. device_event 는 파티션이 없으므로 DELETE

순수 함수(partition_name, month_bounds, expired_partitions)는 마이그레이션과 테스트가
같이 쓴다. DB 를 만지는 건 ensure_partitions / drop_expired_partitions 뿐이다.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.server_settings import runtime

log = logging.getLogger(__name__)

TABLE = "telemetry"
_NAME_RE = re.compile(r"^telemetry_(\d{4})(\d{2})$")


def partition_name(year: int, month: int) -> str:
    return f"{TABLE}_{year:04d}{month:02d}"


def month_bounds(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    """[해당 월 1일 00:00 UTC, 다음 달 1일 00:00 UTC)."""
    start = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
    end = (
        dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc)
        if month == 12
        else dt.datetime(year, month + 1, 1, tzinfo=dt.timezone.utc)
    )
    return start, end


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def partition_ddl(year: int, month: int) -> str:
    """CREATE TABLE IF NOT EXISTS … PARTITION OF telemetry FOR VALUES FROM … TO …"""
    start, end = month_bounds(year, month)
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(year, month)} PARTITION OF {TABLE} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def wanted_partitions(now: dt.datetime) -> list[tuple[int, int]]:
    """이번 달과 다음 달."""
    now_utc = now.astimezone(dt.timezone.utc)
    return [(now_utc.year, now_utc.month), add_months(now_utc.year, now_utc.month, 1)]


def expired_partitions(names: list[str], now: dt.datetime, retention_months: int) -> list[str]:
    """보존 기간을 넘긴 파티션 이름. `retention_months` 개월 전 달까지는 남긴다.

    예: now=2026-09, retention=13 → 2025-08 까지 보존, 2025-07 이하 DROP.
    """
    now_utc = now.astimezone(dt.timezone.utc)
    keep_from = add_months(now_utc.year, now_utc.month, -retention_months)
    expired: list[str] = []
    for name in names:
        match = _NAME_RE.match(name)
        if match is None:
            continue
        year, month = int(match.group(1)), int(match.group(2))
        if (year, month) < keep_from:
            expired.append(name)
    return sorted(expired)


async def list_partitions(conn: AsyncConnection) -> list[str]:
    rows = await conn.execute(
        text(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = :parent"
        ),
        {"parent": TABLE},
    )
    return [row[0] for row in rows]


async def ensure_partitions(conn: AsyncConnection, now: dt.datetime | None = None) -> list[str]:
    """이번 달·다음 달 파티션을 만든다(있으면 통과). 만든/확인한 이름을 돌려준다."""
    now = now or dt.datetime.now(dt.timezone.utc)
    names: list[str] = []
    for year, month in wanted_partitions(now):
        await conn.execute(text(partition_ddl(year, month)))
        names.append(partition_name(year, month))
    return names


async def drop_expired_partitions(
    conn: AsyncConnection, now: dt.datetime | None = None
) -> list[str]:
    now = now or dt.datetime.now(dt.timezone.utc)
    existing = await list_partitions(conn)
    expired = expired_partitions(existing, now, runtime.telemetry_retention_months)
    for name in expired:
        await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        log.warning("telemetry 파티션 DROP: %s (보존 %d개월 초과)", name,
                    runtime.telemetry_retention_months)
    return expired


async def purge_device_events(conn: AsyncConnection, now: dt.datetime | None = None) -> int:
    """device_event 보존 기간(서버 설정 event_months) 초과분 삭제. DELETE 로 충분하다."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=runtime.retention_days("event_months"))
    result = await conn.execute(
        text("DELETE FROM device_event WHERE received_at < :cutoff"), {"cutoff": cutoff}
    )
    return int(result.rowcount or 0)


async def purge_closed_alarms(conn: AsyncConnection, now: dt.datetime | None = None) -> int:
    """닫힌 지 보관 기간(서버 설정 alarm_months) 넘은 알람 이력 삭제(ADR-009)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=runtime.retention_days("alarm_months"))
    result = await conn.execute(
        text("DELETE FROM alarm WHERE closed_at IS NOT NULL AND closed_at < :cutoff"),
        {"cutoff": cutoff},
    )
    return int(result.rowcount or 0)


async def purge_commands(conn: AsyncConnection, now: dt.datetime | None = None) -> int:
    """보낸 지 보관 기간(command_months) 넘은 원격 명령·대상·응답 삭제(문제점 27번).
    단말 행의 마지막 명령 결과는 남는다."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=runtime.retention_days("command_months"))
    old = "SELECT seq FROM command WHERE sent_at < :cutoff"
    await conn.execute(text(f"DELETE FROM command_ack WHERE seq IN ({old})"), {"cutoff": cutoff})
    await conn.execute(text(f"DELETE FROM command_target WHERE seq IN ({old})"), {"cutoff": cutoff})
    result = await conn.execute(text("DELETE FROM command WHERE sent_at < :cutoff"),
                                {"cutoff": cutoff})
    return int(result.rowcount or 0)


async def purge_settings_history(conn: AsyncConnection, now: dt.datetime | None = None) -> int:
    """단말 설정 변경 이력 보관 기간(settings_history_months) 초과분 삭제.
    현재 값(device_settings)은 그대로."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=runtime.retention_days("settings_history_months"))
    result = await conn.execute(
        text("DELETE FROM device_settings_history WHERE changed_at < :cutoff"), {"cutoff": cutoff}
    )
    return int(result.rowcount or 0)


async def purge_deploys(conn: AsyncConnection, now: dt.datetime | None = None) -> int:
    """그룹 스케줄 보낸 기록(deploy_job·deploy_item) 보관 기간(deploy_months) 초과분 삭제.
    단말별 적용 상태(device_schedule)는 남기되, 지운 기록을 가리키는 job_id 만 비운다."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=runtime.retention_days("deploy_months"))
    old = "SELECT id FROM deploy_job WHERE created_at < :cutoff"
    await conn.execute(text(f"UPDATE device_schedule SET job_id = NULL WHERE job_id IN ({old})"),
                       {"cutoff": cutoff})
    await conn.execute(text(f"DELETE FROM deploy_item WHERE job_id IN ({old})"), {"cutoff": cutoff})
    result = await conn.execute(text("DELETE FROM deploy_job WHERE created_at < :cutoff"),
                                {"cutoff": cutoff})
    return int(result.rowcount or 0)


async def run() -> None:
    """스케줄러·기동 진입점. 실패해도 예외를 올리지 않는다(다음 실행이 따라잡는다).
    보관 기간은 서버 설정 "기록 보관 기간"(문제점 27번)."""
    from app.db import engine

    try:
        async with engine.begin() as conn:
            created = await ensure_partitions(conn)
            dropped = await drop_expired_partitions(conn)
            purged = await purge_device_events(conn)
            alarms = await purge_closed_alarms(conn)
            cmds = await purge_commands(conn)
            hist = await purge_settings_history(conn)
            deploys = await purge_deploys(conn)
        log.info("보관 점검: 파티션 유지 %s, DROP %s, "
                 "이벤트 %d·알람 이력 %d·명령 %d·설정 이력 %d·보낸 기록 %d건 삭제",
                 created, dropped, purged, alarms, cmds, hist, deploys)
    except Exception:  # noqa: BLE001
        log.exception("파티션 점검 실패 (다음 주기에 재시도)")
