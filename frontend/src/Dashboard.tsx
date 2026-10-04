import { useCallback, useEffect, useMemo, useState } from "react";
import { AlarmPage, AlarmTab, api, Device, DeviceCounts, DeviceList as DeviceListRes, EnergyToday, Health, MapPoint, STATES, errorText } from "./api";
import { co2Text, kwh2, localTime, relTime, str, volt1, watt1 } from "./format";
import { Battery, Card, LampPair, OnlineMark, PlaceholderCard, StateBadge, nf, stateLabel } from "./ui";
import { DeviceMap, PIN, PinIcon } from "./KakaoMap";
import { Pager, RemoteBadge, shortPath } from "./DeviceList";
import { TAB_LABEL, alarmValue } from "./Alarms";
import { CommandForm, CommandResult, durText, useLedBasis } from "./Command";
import EnergyCards from "./Energy";

const REFRESH_MS = 30_000;
const SAMPLE = 500; // 목록 API 최대 size. ACTIVE 가 이보다 많으면 "표본 500대"

interface Props {
  counts: DeviceCounts | null;
  total: number | null;
  health: Health | null;
  tick: number;
  onSelect: (uuid: string) => void;
  /** 게스트(문제점 21번) — 보기만. 링크·행 클릭·상세 버튼 없음, 지도는 움직일 수 있다. */
  readOnly?: boolean;
}

/** 조치 필요 카드의 점 색 — 알람 등급. */
const SEV_DOT: Record<string, string> = { warn: "var(--alarm)", caution: "var(--warn)", info: "var(--blue)" };

/** 배터리 잔량 구간 — 목업의 5구간 대신 사양서 화면 기준 4구간. */
const BUCKETS: [string, number, number, string][] = [
  ["0~20%", 0, 20, "var(--alarm)"],
  ["20~50%", 20, 50, "var(--warn)"],
  ["50~80%", 50, 80, "var(--use)"],
  ["80~100%", 80, 101, "var(--ok)"],
];

/** 대시보드 — 목업 1~3행. 목록 counts 와 ACTIVE 표본(최대 500대)으로 만든다. 없는 것은 자리만. */
export default function Dashboard({ counts, total, health, tick, onSelect, readOnly = false }: Props) {
  const [active, setActive] = useState<Device[]>([]);
  const [activeTotal, setActiveTotal] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<AlarmTab | "all">("all");
  const [alarms, setAlarms] = useState<AlarmPage | null>(null);
  const [alarmErr, setAlarmErr] = useState<string | null>(null);

  // 조치 필요 = 서버 알람(S-24, ADR-009). 열린 알람을 등급 → 최신순으로 50건.
  useEffect(() => {
    let alive = true;
    const load = () =>
      api.alarms({ status: "open", tab: tab === "all" ? undefined : tab, size: 50 })
        .then((r) => alive && (setAlarms(r), setAlarmErr(null)))
        .catch((e) => alive && setAlarmErr(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [tab, tick]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const a = await api.listDevices({ page: 1, size: SAMPLE, state: "ACTIVE" });
        if (!alive) return;
        setActive(a.items);
        setActiveTotal(a.total);
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

  // 조명 상태: last_telemetry.on. 통신 두절은 마지막 보고와 관계없이 소등(문제점 15번).
  const lamp = useMemo(() => {
    let on = 0, off = 0, unknown = 0;
    for (const d of active) {
      if (!d.is_online) { off++; continue; }
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


  const dotOf = (ok: boolean | undefined) => (ok === undefined ? "var(--off)" : ok ? "var(--ok)" : "var(--alarm)");
  const sampleNote = sampled ? `표본 ${SAMPLE}대` : `ACTIVE ${nf(activeTotal)}대`;

  return (
    <div className={`content${readOnly ? " ro" : ""}`}>
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
        <Card title="조명 상태" meta={error ? <span className="err">{error}</span> : sampleNote}>
          <div className="sbar">
            <i style={{ flex: lamp.on, background: "var(--lamp)" }} />
            <i style={{ flex: lamp.off, background: "var(--seg-off)" }} />
            <i style={{ flex: lamp.unknown, background: "var(--line)" }} />
          </div>
          <div className="keys k3">
            <div className="key"><span className="l"><span className="bulb on" />점등</span><span className="v">{nf(lamp.on)}</span><span className="m">온라인 · on=1</span></div>
            <div className="key"><span className="l"><span className="bulb" />소등</span><span className="v">{nf(lamp.off)}</span><span className="m">on=0 · 통신 두절</span></div>
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
      <MapCard onSelect={onSelect} tick={tick} />
      <Card title="조치 필요" className="h500" meta={alarmErr ? <span className="err">{alarmErr}</span> : <a className="btn sm" href="#alarms">알람 화면</a>}>
        <div className="tabs">
          {(["all", "fault", "comm", "pending", "config", "local"] as const).map((k) => (
            <button key={k} type="button" aria-pressed={tab === k} onClick={() => setTab(k)}>
              {TAB_LABEL[k]}<b>{nf(alarms?.counts?.[k] ?? 0)}</b>
            </button>
          ))}
        </div>
        <ul className="queue">
          {(alarms?.items ?? []).map((a) => (
            <li key={a.id} tabIndex={0} onClick={() => onSelect(a.uuid)} onKeyDown={(e) => e.key === "Enter" && onSelect(a.uuid)}>
              <span className="dot" style={{ background: SEV_DOT[a.severity] ?? "var(--off)" }} />
              <div style={{ minWidth: 0 }}>
                <div className="t">{a.label}{alarmValue(a) ? ` — ${alarmValue(a)}` : ""}</div>
                <div className="d">{`${a.site ?? "(장소 없음)"} · ${a.uuid}`}</div>
              </div>
              <span className="w">{relTime(a.first_seen_at)}</span>
            </li>
          ))}
          {alarms && alarms.items.length === 0 && <li className="empty">조치할 단말이 없습니다.</li>}
        </ul>
      </Card>

      {/* 3행 */}
      {/* 3행 — 발전과 사용 · 에너지 합계(문제점 29번, 단말 일일 값 기반) */}
      <EnergyCards tick={tick} />

      {/* 4행 */}
      <PlaceholderCard title="최근 이벤트" meta="전체 단말" className="full h200" stage="6차" note="전체 이벤트 조회 API(GET /api/events) 추가 필요 — 지금은 단말별 이벤트만 드로어에서" />

      {/* 5행 — 단말 목록(문제점 #11) */}
      <DashDevices tick={tick} onSelect={onSelect} readOnly={readOnly} />
    </div>
  );
}


/** 지도 — 좌표가 있는 단말 핀(KakaoMap PIN: 물방울 핀, 몸통 = 통신, 속 = 조명). 핀을 누르면 드로어. */
function MapCard({ onSelect, tick }: { onSelect: (uuid: string) => void; tick: number }) {
  const [points, setPoints] = useState<MapPoint[]>([]);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.mapPoints().then((p) => alive && (setPoints(p), setErr(null))).catch((e) => alive && setErr(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [tick]);
  const pick = useCallback((u: string) => onSelect(u), [onSelect]);
  return (
    <Card title="지도" className="h500" meta={err ? <span className="err">{err}</span> : <span>좌표 있는 단말 {nf(points.length)}대 · 핀 누르면 상세</span>}>
      <DeviceMap points={points} onSelect={pick} height={430} />
      <div className="keys-inline">
        {(["lit", "dark", "offline", "pending"] as const).map((k) => (
          <span key={k}><PinIcon kind={k} />{PIN[k].label}</span>
        ))}
        <span>· 핀에 마우스를 올리면 시설명·배터리 전압</span>
      </div>
    </Card>
  );
}

/** 대시보드 단말 목록(문제점 #11 탭 "2"). 행 아무 데나 누르면 LED 제어 창, "상세" 는 드로어.
 *  발전량·사용량·감축량은 오늘(KST 0시~지금) 누적 — 화면에 보이는 단말만 계산한다(GET /api/devices/energy). */
function DashDevices({ tick, onSelect, readOnly = false }: { tick: number; onSelect: (uuid: string) => void; readOnly?: boolean }) {
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [state, setState] = useState("ACTIVE");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [res, setRes] = useState<DeviceListRes | null>(null);
  const [energy, setEnergy] = useState<Record<string, EnergyToday>>({});
  const [err, setErr] = useState<string | null>(null);
  const [ctl, setCtl] = useState<Device | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    const id = setTimeout(() => (setQ(text.trim()), setPage(1)), 300);
    return () => clearTimeout(id);
  }, [text]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.listDevices({ page, size, q: q || undefined, state: state || undefined });
        if (!alive) return;
        setRes(r);
        setErr(null);
        const e = await api.energyToday(r.items.map((d) => d.uuid));
        if (alive) setEnergy(e);
      } catch (x) {
        if (alive) setErr(errorText(x));
      }
    };
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [page, size, q, state, tick]);

  const rows = res?.items ?? [];
  const total = res?.total ?? 0;
  return (
    <Card title="단말 목록" className="full" meta={<span>{res ? `${nf(total)}대` : ""} · 발전·사용·감축량은 오늘 누적 · 행을 누르면 LED 제어</span>}>
      <div className="tool">
        <input type="search" placeholder="시설명·UUID·주소·지역 검색" aria-label="대시보드 단말 검색" value={text} onChange={(e) => setText(e.target.value)} style={{ width: 260 }} />
        <select value={state} aria-label="승인 상태" onChange={(e) => (setState(e.target.value), setPage(1))}>
          <option value="">전체 상태</option>
          {STATES.map((s) => <option key={s} value={s}>{stateLabel(s)}</option>)}
        </select>
        <span className="sp" />
        <select value={size} aria-label="페이지 크기" onChange={(e) => (setSize(Number(e.target.value)), setPage(1))}>
          {[20, 50, 100].map((n) => <option key={n} value={n}>{n}개씩</option>)}
        </select>
      </div>
      {err && <div className="err">{err}</div>}
      {note && <div className="okl">{note}</div>}
      <div className="tw">
        <table className="list">
          <thead>
            <tr>
              <th>상태</th><th>시설명</th><th>지역</th><th>통신</th><th>조명</th><th>원격</th><th>배터리</th><th>배터리 전압</th>
              <th className="n">발전전력</th><th className="n">일일발전량</th><th className="n">일일사용량</th><th className="n">온실가스 감축량</th><th>상세정보</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => {
              const e = energy[d.uuid];
              return (
                <tr key={d.uuid} data-click={readOnly ? undefined : true} onClick={readOnly ? undefined : () => setCtl(d)} title={readOnly ? undefined : "누르면 LED 제어"}>
                  <td><StateBadge state={d.state} /></td>
                  <td>{str(d.site)}</td>
                  <td title={d.node_path ?? ""}>{d.node_id ? shortPath(d) : ""}</td>
                  <td><OnlineMark on={d.is_online} /></td>
                  <td><LampPair t={d.last_telemetry} online={d.is_online} /></td>
                  <td><RemoteBadge d={d} onReleased={setNote} /></td>
                  <td><Battery sc={d.last_telemetry?.sc} /></td>
                  <td>{volt1(d.last_telemetry?.bv)}</td>
                  <td className="n">{watt1(d.last_telemetry?.pp)}</td>
                  {/* 단말이 보낸 금일 발전·사용(eg·eu, 문제점 23번). MPPT 무응답이면 "값 없음", 모르면 공백. */}
                  <td className="n" title={e?.source === "server" ? "옛 펌웨어 — 서버가 계산한 값" : undefined}>{e?.no_value ? <span className="muted">값 없음</span> : kwh2(e?.gen_wh)}</td>
                  <td className="n">{e?.no_value ? <span className="muted">값 없음</span> : kwh2(e?.use_wh)}</td>
                  <td className="n">{e?.no_value ? <span className="muted">값 없음</span> : co2Text(e?.co2_g)}</td>
                  <td>{!readOnly && <button type="button" className="btn sm" onClick={(x) => (x.stopPropagation(), onSelect(d.uuid))}>상세</button>}</td>
                </tr>
              );
            })}
            {rows.length === 0 && <tr><td colSpan={13} className="empty">{res ? "조건에 맞는 단말이 없습니다." : "불러오는 중…"}</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager page={page} pages={Math.max(1, Math.ceil(total / size))} total={total} size={size} onPage={setPage} />
      {ctl && <LedControl d={ctl} onClose={() => setCtl(null)} onDetail={() => (setCtl(null), onSelect(ctl.uuid))} />}
    </Card>
  );
}

/** LED 제어 창 — 그 단말 하나에 개별 COMMAND(점등·소등·밝기·스케줄 복귀, 채널, 유지시간). */
function LedControl({ d, onClose, onDetail }: { d: Device; onClose: () => void; onDetail: () => void }) {
  const [seq, setSeq] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const t = d.last_telemetry;
  // 밝기 = 설치 기준 대비 비율, 처음 값 = 지금 비율(문제점 12·19번).
  const basis = useLedBasis(d.uuid, d.is_online ? t : null, d.last_telemetry_at);
  return (
    <div className="modal-ov" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal wide" role="dialog" aria-label="LED 제어">
        <div className="dh">
          <div style={{ minWidth: 0 }}>
            <h3>LED 제어 · {d.site ?? d.uuid}</h3>
            <div className="u">{d.node_path ?? ""} · {d.uuid}</div>
          </div>
          <button type="button" className="x" onClick={onClose} aria-label="닫기">✕</button>
        </div>
        <div className="db">
          <div className="bar2">
            <LampPair t={t} online={d.is_online} />
            <RemoteBadge d={d} onReleased={setMsg} />
            <span className="sp" />
            <span className="cap">마지막 수신 {localTime(d.last_telemetry_at)}</span>
          </div>
          <div className="cap">원격 명령 뒤 점등 상태는 단말이 2초 뒤 보내는 Telemetry 로 바뀐다. 단말은 송신 직후에만 명령을 받아 수십 초~최대 약 5분 걸릴 수 있다.</div>
          {msg && <div className="okl">{msg}</div>}
          <CommandForm target={{ kind: "device", id: d.uuid }} targetLabel={d.site ?? d.uuid}
            basis={basis}
            blocked={d.state !== "ACTIVE" ? "운영(ACTIVE) 단말에만 보낼 수 있다" : !d.is_online ? "오프라인 단말에는 보낼 수 없다 — 다시 접속하면 보낼 수 있다" : null}
            onSent={(c) => (setSeq(c.seq), setMsg(`명령 #${c.seq} 발행함${c.payload.dur ? ` · 유지 ${durText(Number(c.payload.dur))}` : ""}`))} />
          {seq !== null && <CommandResult seq={seq} onClose={() => setSeq(null)} />}
          <div className="bar2"><span className="sp" /><button type="button" className="btn" onClick={onDetail}>단말 상세 열기</button></div>
        </div>
      </div>
    </div>
  );
}
