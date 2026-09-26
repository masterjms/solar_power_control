import { useEffect, useMemo, useState } from "react";
import { api, Device, DeviceCounts, Health, errorText } from "./api";
import { relTime, str } from "./format";
import { Card, PlaceholderCard, nf } from "./ui";

const REFRESH_MS = 30_000;
const SAMPLE = 500; // 목록 API 최대 size. ACTIVE 가 이보다 많으면 "표본 500대"

interface Props {
  counts: DeviceCounts | null;
  total: number | null;
  health: Health | null;
  tick: number;
  onSelect: (uuid: string) => void;
}

type IssueKind = "pending" | "offline" | "mismatch";
interface Issue {
  kind: IssueKind;
  d: Device;
  title: string;
  when: string | null;
}
const ISSUE: Record<IssueKind, [string, string]> = {
  pending: ["승인 대기", "var(--blue)"],
  offline: ["통신 두절", "var(--off)"],
  mismatch: ["설정 불일치", "var(--warn)"],
};

/** 배터리 잔량 구간 — 목업의 5구간 대신 사양서 화면 기준 4구간. */
const BUCKETS: [string, number, number, string][] = [
  ["0~20%", 0, 20, "var(--alarm)"],
  ["20~50%", 20, 50, "var(--warn)"],
  ["50~80%", 50, 80, "var(--use)"],
  ["80~100%", 80, 101, "var(--ok)"],
];

/** 대시보드 — 목업 1~3행. 목록 counts 와 ACTIVE 표본(최대 500대)으로 만든다. 없는 것은 자리만. */
export default function Dashboard({ counts, total, health, tick, onSelect }: Props) {
  const [active, setActive] = useState<Device[]>([]);
  const [activeTotal, setActiveTotal] = useState<number | null>(null);
  const [pending, setPending] = useState<Device[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"all" | IssueKind>("all");

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [a, p] = await Promise.all([
          api.listDevices({ page: 1, size: SAMPLE, state: "ACTIVE" }),
          api.listDevices({ page: 1, size: SAMPLE, state: "PENDING" }),
        ]);
        if (!alive) return;
        setActive(a.items);
        setActiveTotal(a.total);
        setPending(p.items);
        setError(null);
      } catch (e) {
        if (alive) setError(errorText(e));
      }
    };
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [tick]);

  const sampled = activeTotal !== null && activeTotal > SAMPLE;
  const online = counts?.online ?? null;
  const offline = total !== null && online !== null ? total - online : null;
  const stopped = counts ? counts.SUSPENDED + counts.REJECTED : null;

  // 조명 상태: last_telemetry.on
  const lamp = useMemo(() => {
    let on = 0, off = 0, unknown = 0;
    for (const d of active) {
      const v = d.last_telemetry?.on;
      if (v === 1) on++;
      else if (v === 0) off++;
      else unknown++;
    }
    return { on, off, unknown };
  }, [active]);

  // 배터리 잔량 구간
  const batt = useMemo(() => {
    const cnt = BUCKETS.map(() => 0);
    let sum = 0, n = 0;
    for (const d of active) {
      const sc = d.last_telemetry?.sc;
      if (sc === null || sc === undefined) continue;
      sum += sc;
      n++;
      const i = BUCKETS.findIndex(([, lo, hi]) => sc >= lo && sc < hi);
      if (i >= 0) cnt[i]++;
    }
    return { cnt, avg: n ? Math.round(sum / n) : null, n, max: Math.max(1, ...cnt) };
  }, [active]);

  // 조치 필요
  const issues = useMemo<Issue[]>(() => {
    const list: Issue[] = [];
    for (const d of pending) list.push({ kind: "pending", d, title: `승인 대기 — ${str(d.device_model)} F/W ${str(d.fw)}`, when: d.last_register_at ?? d.created_at });
    for (const d of active) {
      if (!d.is_online) list.push({ kind: "offline", d, title: `통신 두절 — 마지막 수신 ${relTime(d.last_seen_at)}`, when: d.offline_at ?? d.last_seen_at });
      if (d.config_mismatch) list.push({ kind: "mismatch", d, title: `설정 불일치 — ti ${d.ti_effective}/${str(d.ti_device)} · ka ${d.ka_effective}/${str(d.ka_device)}`, when: d.last_telemetry_at });
    }
    return list;
  }, [pending, active]);
  const issueCount = (k: "all" | IssueKind) => (k === "all" ? issues.length : issues.filter((i) => i.kind === k).length);
  const shown = issues.filter((i) => tab === "all" || i.kind === tab);

  const dotOf = (ok: boolean | undefined) => (ok === undefined ? "var(--off)" : ok ? "var(--ok)" : "var(--alarm)");
  const sampleNote = sampled ? `표본 ${SAMPLE}대` : `ACTIVE ${nf(activeTotal)}대`;

  return (
    <div className="content">
      {/* 1행 */}
      <div className="pair">
        <Card
          title="단말 상태"
          meta={<><span>{total === null ? "-" : `${nf(total)}대 등록`}</span><a className="btn sm c-blue" href="#pending">승인 대기 {nf(counts?.PENDING)}</a></>}
        >
          <div className="sbar" aria-label="상태 비율">
            <i style={{ flex: online ?? 0, background: "var(--ok)" }} />
            <i style={{ flex: offline ?? 0, background: "var(--off)" }} />
            <i style={{ flex: counts?.PENDING ?? 0, background: "var(--blue)" }} />
            <i style={{ flex: stopped ?? 0, background: "var(--warn)" }} />
          </div>
          <div className="keys">
            <a className="key" href="#devices" title="is_online=true"><span className="l"><span className="dot" style={{ background: "var(--ok)" }} />온라인</span><span className="v c-ok">{nf(online)}</span><span className="m">{total ? `${Math.round(((online ?? 0) / total) * 100)}%` : ""}</span></a>
            <a className="key" href="#devices" title="전체 − 온라인"><span className="l"><span className="dot" style={{ background: "var(--off)" }} />오프라인</span><span className="v c-off">{nf(offline)}</span><span className="m">{total ? `${Math.round(((offline ?? 0) / total) * 100)}%` : ""}</span></a>
            <a className="key" href="#pending"><span className="l"><span className="dot" style={{ background: "var(--blue)" }} />승인 대기</span><span className="v c-blue">{nf(counts?.PENDING)}</span><span className="m">등록·승인 화면</span></a>
            <a className="key" href="#devices"><span className="l"><span className="dot" style={{ background: "var(--warn)" }} />중지·거부</span><span className="v c-warn">{nf(stopped)}</span><span className="m">{counts ? `중지 ${counts.SUSPENDED} · 거부 ${counts.REJECTED} · 폐기 ${counts.RETIRED}` : ""}</span></a>
          </div>
          <div className="hrow">
            <span><span className="dot" style={{ background: dotOf(health?.mqtt_connected) }} />MQTT</span>
            <span><span className="dot" style={{ background: dotOf(health?.db_ok) }} />DB</span>
            <span><span className="dot" style={{ background: dotOf(health?.broker_log_tail) }} />브로커 로그</span>
            {health && <span>버퍼 {health.buffer_pending} · 등록 큐 {health.register_queue}</span>}
          </div>
        </Card>
        <Card title="조명 상태" meta={sampleNote}>
          <div className="sbar">
            <i style={{ flex: lamp.on, background: "var(--lamp)" }} />
            <i style={{ flex: lamp.off, background: "var(--seg-off)" }} />
            <i style={{ flex: lamp.unknown, background: "var(--line)" }} />
          </div>
          <div className="keys k3">
            <div className="key"><span className="l"><span className="bulb on" />점등</span><span className="v">{nf(lamp.on)}</span><span className="m">Telemetry on=1</span></div>
            <div className="key"><span className="l"><span className="bulb" />소등</span><span className="v">{nf(lamp.off)}</span><span className="m">on=0</span></div>
            <div className="key"><span className="l"><span className="dot" style={{ background: "var(--line)" }} />미수신</span><span className="v c-off">{nf(lamp.unknown)}</span><span className="m">Telemetry 없음</span></div>
          </div>
        </Card>
      </div>
      <Card title="배터리 잔량" meta={<>평균 <b style={{ color: "var(--text)" }}>{batt.avg === null ? "-" : `${batt.avg}%`}</b> · {sampleNote}</>}>
        <div className="hist">
          {BUCKETS.map(([label, , , color], i) => (
            <button key={label} type="button" title={`${label} ${batt.cnt[i]}대`}>
              <span>{label}</span>
              <span className="tr"><i style={{ width: `${(batt.cnt[i] / batt.max) * 100}%`, background: color }} /></span>
              <b>{nf(batt.cnt[i])}</b>
            </button>
          ))}
        </div>
        <div className="cap">last_telemetry.sc 기준, 수신 {nf(batt.n)}대</div>
      </Card>

      {/* 2행 */}
      <PlaceholderCard title="지도" meta="법정동별 군집 · 알람 핀" className="h500" stage="7차" note="지도 API 키와 법정동 집계 API 뒤에 붙인다" />
      <Card title="조치 필요" className="h500" meta={error ? <span className="err">{error}</span> : <span>{sampled ? "표본 기준" : ""}</span>}>
        <div className="tabs">
          {(["all", "pending", "offline", "mismatch"] as const).map((k) => (
            <button key={k} type="button" aria-pressed={tab === k} onClick={() => setTab(k)}>
              {k === "all" ? "전체" : ISSUE[k][0]}<b>{issueCount(k)}</b>
            </button>
          ))}
        </div>
        <ul className="queue">
          {shown.map((i) => (
            <li key={`${i.kind}-${i.d.uuid}`} tabIndex={0} onClick={() => onSelect(i.d.uuid)} onKeyDown={(e) => e.key === "Enter" && onSelect(i.d.uuid)}>
              <span className="dot" style={{ background: ISSUE[i.kind][1] }} />
              <div style={{ minWidth: 0 }}>
                <div className="t">{i.title}</div>
                <div className="d">{d_label(i.d)}</div>
              </div>
              <span className="w">{relTime(i.when)}</span>
            </li>
          ))}
          {shown.length === 0 && <li className="empty">조치할 단말이 없습니다.</li>}
        </ul>
      </Card>

      {/* 3행 */}
      <PlaceholderCard title="발전과 사용" meta="최근 7일, MWh" className="h200" stage="7차" note="일 집계(daily rollup)는 있으나 조회 API 가 없다" />
      <PlaceholderCard title="에너지 합계" meta="전체 단말" className="h200" stage="7차" note="금일·누적 발전, 감축량" />

      {/* 4행 */}
      <PlaceholderCard title="최근 이벤트" meta="전체 단말" className="full h200" stage="6차" note="전체 이벤트 조회 API(GET /api/events) 추가 필요 — 지금은 단말별 이벤트만 드로어에서" />
    </div>
  );
}

function d_label(d: Device): string {
  return `${d.site ?? "(장소 없음)"} · ${d.uuid}`;
}
