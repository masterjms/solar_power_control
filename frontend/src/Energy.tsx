// 대시보드 "발전과 사용" 그래프 · "에너지 합계" (문제점 29번). GET /api/energy/summary.
// 날짜별은 단말이 보낸 일일 값(telemetry_daily), 오늘은 지금 eg·eu 합, 누적은 단말 누적 + 오늘. 통계 시작일 전은 없다.
// 외부 그래프 라이브러리 없이 SVG 막대로 그린다(화면 무게 유지).
import { useEffect, useState } from "react";
import { api, EnergySummary, errorText } from "./api";
import { co2Text } from "./format";
import { Card, nf } from "./ui";

const PERIODS: [EnergySummary["period"], string][] = [["7d", "7일"], ["30d", "30일"], ["12m", "12개월"]];
const REFRESH_MS = 60_000;

/** Wh → 보기 좋은 단위. 값이 없으면 공백. */
export function energyText(wh: number | null | undefined): string {
  if (wh === null || wh === undefined) return "";
  const a = Math.abs(wh);
  if (a >= 1e6) return `${(wh / 1e6).toFixed(2)} MWh`;
  if (a >= 1000) return `${(wh / 1000).toFixed(2)} kWh`;
  return `${Math.round(wh)} Wh`;
}

/** 축 단위: 최댓값 기준으로 Wh / kWh / MWh 중 하나. */
function axisUnit(maxWh: number): { div: number; unit: string } {
  if (maxWh >= 1e6) return { div: 1e6, unit: "MWh" };
  if (maxWh >= 1000) return { div: 1000, unit: "kWh" };
  return { div: 1, unit: "Wh" };
}

/** 1·2·5 단계의 눈금 위 값. */
function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= p * m) return p * m;
  return p * 10;
}

function label(day: string, period: string, today: boolean): string {
  if (today && period !== "12m") return "오늘";
  const [y, m, d] = day.split("-");
  return period === "12m" ? `${y.slice(2)}.${Number(m)}` : `${Number(m)}/${Number(d)}`;
}

export default function EnergyCards({ tick }: { tick: number }) {
  const [period, setPeriod] = useState<EnergySummary["period"]>("7d");
  const [data, setData] = useState<EnergySummary | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => api.energySummary(period).then((d) => alive && (setData(d), setErr(null))).catch((e) => alive && setErr(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => { alive = false; clearInterval(id); };
  }, [period, tick]);

  const days = data?.days ?? [];
  const maxWh = Math.max(1, ...days.map((d) => Math.max(d.gen_wh + d.est_gen_wh, d.use_wh)));
  const { div, unit } = axisUnit(maxWh);
  const top = niceMax(maxWh / div);
  const W = 720, H = 190, L = 44, R = 8, T = 10, B = 26;
  const iw = W - L - R, ih = H - T - B;
  const n = Math.max(1, days.length);
  const bw = iw / n;
  const bar = Math.min(18, bw * 0.3);
  const y = (v: number) => T + ih - (v / div / top) * ih;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);

  return (
    <>
      <Card title="발전과 사용" className="h200"
        meta={
          <span className="bar2" style={{ gap: 6 }}>
            <span className="tabs"> {/* 기간 — 단말측 요청: 그래프 위에서 바로 고른다 */}
              {PERIODS.map(([k, l]) => <button key={k} type="button" aria-pressed={period === k} onClick={() => setPeriod(k)}>{l}</button>)}
            </span>
            <span className="cap"><i className="sw gen" />발전 <i className="sw use" />사용 · 연한 색 = 오늘(진행 중)·추정 · {unit}</span>
          </span>
        }>
        {err && <div className="err">{err}</div>}
        {!err && data && days.length === 0 && <div className="muted">통계 시작일({data.since}) 이후 기록이 아직 없습니다.</div>}
        {days.length > 0 && (
          <svg className="ebars" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="발전과 사용 막대 그래프">
            {ticks.map((t) => (
              <g key={t}>
                <line x1={L} x2={W - R} y1={y(t * div)} y2={y(t * div)} className="grid" />
                <text x={L - 6} y={y(t * div) + 4} className="ax" textAnchor="end">{t >= 100 ? t.toFixed(0) : t.toFixed(t < 10 ? 1 : 0)}</text>
              </g>
            ))}
            {days.map((d, i) => {
              const x0 = L + i * bw + (bw - bar * 2 - 4) / 2;
              const faded = d.today;
              const tip = `${d.day}${d.today ? " (오늘, 진행 중)" : ""}\n발전 ${energyText(d.gen_wh)}${d.est_gen_wh ? ` + 추정 ${energyText(d.est_gen_wh)}` : ""}\n사용 ${energyText(d.use_wh)}\n단말 ${d.devices}대`;
              return (
                <g key={d.day} className={faded ? "today" : ""}>
                  <title>{tip}</title>
                  <rect x={x0} y={y(d.gen_wh)} width={bar} height={Math.max(0, T + ih - y(d.gen_wh))} className="gen" rx={2} />
                  {d.est_gen_wh > 0 && (
                    <rect x={x0} y={y(d.gen_wh + d.est_gen_wh)} width={bar} height={Math.max(0, y(d.gen_wh) - y(d.gen_wh + d.est_gen_wh))} className="gen est" rx={2} />
                  )}
                  <rect x={x0 + bar + 4} y={y(d.use_wh)} width={bar} height={Math.max(0, T + ih - y(d.use_wh))} className="use" rx={2} />
                  {(n <= 14 || i % Math.ceil(n / 14) === 0 || d.today) && (
                    <text x={x0 + bar + 2} y={H - 8} className="ax" textAnchor="middle">{label(d.day, period, d.today)}</text>
                  )}
                </g>
              );
            })}
          </svg>
        )}
      </Card>

      <Card title="에너지 합계" className="h200" meta={data ? `전체 단말 ${nf(data.now.total_devices)}대` : ""}>
        {data && (
          <>
            <div className="grid2">
              <div className="met"><div className="l"><i className="sw gen" />금일 발전</div><div className="v">{energyText(data.now.gen_wh) || "0 Wh"}</div></div>
              <div className="met"><div className="l"><i className="sw use" />금일 사용</div><div className="v">{energyText(data.now.use_wh) || "0 Wh"}</div></div>
              <div className="met"><div className="l">누적 발전</div><div className="v">{energyText(data.total.gen_wh) || "0 Wh"}</div><div className="h">{data.since ? `${data.since} 부터` : "기록 시작부터"}</div></div>
              <div className="met"><div className="l">누적 감축</div><div className="v">{co2Text(data.total.co2_kg * 1000) || "0 gCO2eq"}</div><div className="h">발전 × {(data.ghg_kg_per_kwh * 1000).toFixed(1)} g/kWh (서버 설정)</div></div>
            </div>
            <div className="cap" style={{ marginTop: 8 }}>
              금일 반영 {nf(data.now.reported)}/{nf(data.now.total_devices)}대
              {data.now.no_report ? ` · 오늘 보고 없음 ${nf(data.now.no_report)}대` : ""}
              {data.now.mppt_offline ? ` · MPPT 무응답 ${nf(data.now.mppt_offline)}대` : ""}
            </div>
          </>
        )}
        {err && <div className="err">{err}</div>}
      </Card>
    </>
  );
}
