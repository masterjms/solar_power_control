"""오늘(KST 0시~지금) 단말별 발전량·사용량·온실가스 감축량 — 대시보드 단말 목록(문제점 #11).

전날까지는 일 집계(telemetry_daily, tasks/daily_rollup)가 있지만 "오늘"은 아직 집계 전이다.
화면 한 페이지(최대 500대) 단말만 골라 daily_rollup 과 **같은 사다리꼴 적분**을 오늘 구간에 돌린다.
1만 대 전부가 아니라 화면에 보이는 uuid 만이라 가볍다(대당 하루 최대 약 150행).

    발전량 gen_wh = Σ (pp_prev + pp)/2/100 [W] × Δt[h]          (pp 는 W×100)
    사용량 use_wh = Σ (li_prev·bv_prev + li·bv)/2/10000 [W] × Δt[h]  (li A×100, bv V×100)
    감축량 co2_g  = gen_wh/1000 [kWh] × GHG_KG_PER_KWH × 1000

Δt 가 MAX_GAP_SEC 를 넘는 구간(통신 두절)은 적분하지 않는다(발전량을 지어내지 않는다).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.tasks.daily_rollup import KST, MAX_GAP_SEC

_TODAY_SQL = text(
    """
    WITH s AS (
      SELECT uuid, received_at, pp, li, bv,
             lag(received_at) OVER w AS prev_t,
             lag(pp) OVER w AS prev_pp, lag(li) OVER w AS prev_li, lag(bv) OVER w AS prev_bv
      FROM telemetry
      WHERE received_at >= :start AND uuid = ANY(:uuids)
      WINDOW w AS (PARTITION BY uuid ORDER BY received_at)
    ),
    seg AS (
      SELECT uuid, pp, li, bv, prev_pp, prev_li, prev_bv,
             CASE WHEN prev_t IS NULL THEN NULL
                  WHEN EXTRACT(EPOCH FROM received_at - prev_t) > :max_gap THEN NULL
                  ELSE EXTRACT(EPOCH FROM received_at - prev_t) END AS dt_sec
      FROM s
    )
    SELECT uuid,
           count(*) AS samples,
           sum(CASE WHEN dt_sec IS NULL THEN 0
                    ELSE (coalesce(pp,0) + coalesce(prev_pp,0)) / 2.0 / 100.0 * dt_sec / 3600.0 END) AS gen_wh,
           sum(CASE WHEN dt_sec IS NULL THEN 0
                    ELSE (coalesce(li,0) * coalesce(bv,0) + coalesce(prev_li,0) * coalesce(prev_bv,0))
                         / 2.0 / 10000.0 * dt_sec / 3600.0 END) AS use_wh
    FROM seg GROUP BY uuid
    """
)


def today_start_kst(now: dt.datetime) -> dt.datetime:
    local = now.astimezone(KST)
    return dt.datetime.combine(local.date(), dt.time(0), tzinfo=KST)


def co2_g(gen_wh: float | None) -> float | None:
    return None if gen_wh is None else gen_wh * settings.ghg_kg_per_kwh


async def today(db: AsyncSession, uuids: list[str], now: dt.datetime) -> dict[str, dict]:
    """{uuid: {gen_wh, use_wh, co2_g, samples}}. 오늘 Telemetry 가 없는 단말은 빠진다(화면은 공백)."""
    if not uuids:
        return {}
    rows = await db.execute(_TODAY_SQL, {
        "start": today_start_kst(now), "uuids": uuids, "max_gap": MAX_GAP_SEC,
    })
    out: dict[str, dict] = {}
    for uuid, samples, gen, use in rows:
        g = float(gen or 0.0)
        out[uuid] = {"samples": int(samples), "gen_wh": round(g, 2),
                     "use_wh": round(float(use or 0.0), 2), "co2_g": round(co2_g(g) or 0.0, 2)}
    return out
