"""오늘(KST 0시~지금) 단말별 발전량·사용량·온실가스 감축량 — 대시보드 단말 목록(문제점 #11, #23).

2026-10-01(문제점 23번, 사양서 §1.1.6 단말 빌드 2026-10-01-1): 단말이 Telemetry 에 일일 전력량을 싣는다.
    eg 금일 발전 · eu 금일 사용 · yg 전일 발전 · yu 전일 사용 — 단위 kWh×100(160 = 1.60 kWh, 10 Wh 단위).
적산·자정 리셋은 PowerMPPT 보드가 한다. **서버는 계산하지 않고 받은 값을 표시만 한다**:
    일일발전량 = 최신 eg × 10 Wh,  일일사용량 = 최신 eu × 10 Wh,  감축량 = 발전량 × GHG_KG_PER_KWH
  · MPPT 무응답(er 0x0010)이면 네 값이 모두 0 으로 온다 → 0 이 아니라 "값 없음"(no_value).
  · 마지막 Telemetry 가 오늘 것이 아니면(어제부터 통신 두절) 오늘 값은 모른다 → 비움.

eg 가 없는 Telemetry(옛 펌웨어)를 보내는 단말만 예전처럼 서버가 적분한다(source="server"):
    발전량 gen_wh = Σ (pp_prev + pp)/2/100 [W] × Δt[h]          (pp 는 W×100)
    사용량 use_wh = Σ (li_prev·bv_prev + li·bv)/2/10000 [W] × Δt[h]  (li A×100, bv V×100)
Δt 가 MAX_GAP_SEC 를 넘는 구간(통신 두절)은 적분하지 않는다(발전량을 지어내지 않는다).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.device import Device
from app.tasks.daily_rollup import KST, MAX_GAP_SEC

#: er bit — MPPT 보드 무응답(사양서 §16.2.2).
ER_MPPT_OFFLINE = 0x0010
#: kWh×100 → Wh
WH_PER_UNIT = 10

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


def _int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def from_device(
    tm: dict[str, Any] | None, tm_at: dt.datetime | None, today_start: dt.datetime
) -> dict[str, Any] | None:
    """단말이 보낸 일일 전력량으로 만든 오늘 값. eg 가 없으면(옛 펌웨어·Telemetry 없음) None → 서버 적분.

    반환 {gen_wh, use_wh, co2_g, no_value, source="device"}. 값을 모르면 None 으로 둔다(화면은 공백),
    MPPT 무응답이면 no_value=True(화면은 "값 없음").
    """
    if not tm or "eg" not in tm:
        return None
    blank = {"gen_wh": None, "use_wh": None, "co2_g": None, "no_value": False, "source": "device"}
    if (_int(tm.get("er")) or 0) & ER_MPPT_OFFLINE:
        return {**blank, "no_value": True}
    if tm_at is None or tm_at < today_start:
        return blank  # 마지막 보고가 어제 이전 — 오늘 값은 모른다
    eg, eu = _int(tm.get("eg")), _int(tm.get("eu"))
    gen = None if eg is None else float(eg * WH_PER_UNIT)
    use = None if eu is None else float(eu * WH_PER_UNIT)
    g = co2_g(gen)
    return {"gen_wh": gen, "use_wh": use, "co2_g": None if g is None else round(g, 2),
            "no_value": False, "source": "device"}


async def today(db: AsyncSession, uuids: list[str], now: dt.datetime) -> dict[str, dict]:
    """{uuid: {gen_wh, use_wh, co2_g, samples, no_value, source}}.

    단말 값이 있으면 그것, 없는 단말(옛 펌웨어)만 적분. 오늘 Telemetry 가 없는 옛 펌웨어 단말은 빠진다(화면은 공백).
    """
    if not uuids:
        return {}
    start = today_start_kst(now)
    out: dict[str, dict] = {}
    legacy: list[str] = []
    rows = await db.execute(
        select(Device.uuid, Device.last_telemetry, Device.last_telemetry_at)
        .where(Device.uuid.in_(uuids))
    )
    for uuid, tm, tm_at in rows:
        got = from_device(tm, tm_at, start)
        if got is None:
            legacy.append(uuid)
        else:
            out[uuid] = {"samples": 0, **got}
    if legacy:
        rows = await db.execute(_TODAY_SQL, {"start": start, "uuids": legacy, "max_gap": MAX_GAP_SEC})
        for uuid, samples, gen, use in rows:
            g = float(gen or 0.0)
            out[uuid] = {"samples": int(samples), "gen_wh": round(g, 2),
                         "use_wh": round(float(use or 0.0), 2), "co2_g": round(co2_g(g) or 0.0, 2),
                         "no_value": False, "source": "server"}
    return out
