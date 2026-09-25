"""일 집계 — telemetry → telemetry_daily (docs/03 §telemetry_daily).

**일 단위 발전량/소비량 집계는 서버가 한다**(사양서 §1.1.6 "서버가 유의할 점"). MPPT 보드에
RTC 가 없어 단말은 달력 날짜를 모른다.

날짜는 KST 다. 하루 = [KST 00:00, 다음날 KST 00:00) 를 UTC 로 바꿔 telemetry 를 자른다.
매일 00:30 KST 에 전날을 집계하고, REST(POST /api/admin/rollup?day=) 로 아무 날이나 다시
돌릴 수 있다(ON CONFLICT 로 덮어쓴다).

계산은 전부 SQL 이다. 1만 대 × 144건 = 하루 144만 행을 파이썬으로 끌어오면 메모리와
시간이 문제다. 윈도 함수 lag() 로 직전 표본을 붙여 사다리꼴 적분한다:

    pp_wh  = Σ (pp_prev + pp) / 2 / 100 [W] × Δt [h]        (pp 는 W×100)
    li_ah  = Σ (li_prev + li) / 2 / 100 [A] × Δt [h]        (li 는 A×100)
    on_minutes = Σ Δt [min]  where on_prev = 1               (직전 표본부터 이번 표본까지
                                                             직전 상태가 유지됐다고 본다)

Δt 가 MAX_GAP_SEC 를 넘는 구간(단말이 꺼져 있었거나 통신 두절)은 적분에서 뺀다 —
2시간 전 값과 지금 값 사이를 직선으로 잇는 것은 발전량을 지어내는 것이다.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger(__name__)

KST = dt.timezone(dt.timedelta(hours=9))
#: 표본 간격이 이보다 크면 그 구간은 적분하지 않는다.
MAX_GAP_SEC = 7200

_ROLLUP_SQL = text(
    """
    WITH s AS (
      SELECT uuid, received_at, pp, li, "on", bv, sc,
             lag(received_at) OVER w AS prev_t,
             lag(pp)          OVER w AS prev_pp,
             lag(li)          OVER w AS prev_li,
             lag("on")        OVER w AS prev_on
      FROM telemetry
      WHERE received_at >= :start AND received_at < :end
      WINDOW w AS (PARTITION BY uuid ORDER BY received_at)
    ),
    seg AS (
      SELECT uuid, received_at, pp, li, "on", bv, sc, prev_pp, prev_li, prev_on,
             CASE
               WHEN prev_t IS NULL THEN NULL
               WHEN EXTRACT(EPOCH FROM received_at - prev_t) > :max_gap THEN NULL
               ELSE EXTRACT(EPOCH FROM received_at - prev_t)
             END AS dt_sec
      FROM s
    ),
    agg AS (
      SELECT uuid,
             count(*)                                                       AS samples,
             sum(CASE WHEN dt_sec IS NULL THEN 0
                      ELSE (coalesce(pp,0) + coalesce(prev_pp,0)) / 2.0 / 100.0
                           * dt_sec / 3600.0 END)                          AS pp_wh,
             sum(CASE WHEN dt_sec IS NULL THEN 0
                      ELSE (coalesce(li,0) + coalesce(prev_li,0)) / 2.0 / 100.0
                           * dt_sec / 3600.0 END)                          AS li_ah,
             min(bv) AS bv_min, max(bv) AS bv_max,
             min(sc) AS sc_min, max(sc) AS sc_max,
             sum(CASE WHEN dt_sec IS NULL OR prev_on IS DISTINCT FROM 1 THEN 0
                      ELSE dt_sec / 60.0 END)                              AS on_minutes
      FROM seg
      GROUP BY uuid
    )
    INSERT INTO telemetry_daily
      (uuid, day, samples, pp_wh, li_ah, bv_min, bv_max, sc_min, sc_max, on_minutes)
    SELECT uuid, :day, samples, pp_wh, li_ah, bv_min, bv_max, sc_min, sc_max,
           round(on_minutes)::int
    FROM agg
    ON CONFLICT (uuid, day) DO UPDATE SET
      samples = EXCLUDED.samples, pp_wh = EXCLUDED.pp_wh, li_ah = EXCLUDED.li_ah,
      bv_min = EXCLUDED.bv_min, bv_max = EXCLUDED.bv_max,
      sc_min = EXCLUDED.sc_min, sc_max = EXCLUDED.sc_max,
      on_minutes = EXCLUDED.on_minutes
    """
)


def kst_day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """KST 하루의 [시작, 끝) 을 UTC 로."""
    start = dt.datetime(day.year, day.month, day.day, tzinfo=KST)
    end = start + dt.timedelta(days=1)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def yesterday_kst(now: dt.datetime | None = None) -> dt.date:
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now.astimezone(KST) - dt.timedelta(days=1)).date()


async def rollup_day(conn: AsyncConnection, day: dt.date) -> int:
    """하루치 집계. 반환값은 갱신된 단말 수."""
    start, end = kst_day_bounds(day)
    result = await conn.execute(
        _ROLLUP_SQL, {"start": start, "end": end, "day": day, "max_gap": MAX_GAP_SEC}
    )
    return int(result.rowcount or 0)


async def run(day: dt.date | None = None) -> int:
    """스케줄러·REST 진입점. day 를 안 주면 KST 어제."""
    from app.db import engine

    day = day or yesterday_kst()
    try:
        async with engine.begin() as conn:
            count = await rollup_day(conn, day)
        log.info("일 집계 %s: 단말 %d대", day, count)
        return count
    except Exception:  # noqa: BLE001
        log.exception("일 집계 실패 %s (수동 재실행: POST /api/admin/rollup?day=%s)", day, day)
        raise
