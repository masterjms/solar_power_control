// 5차 원격 제어 — 명령 폼 · 보내기 전 확인 · 결과(응답 집계) · 명령 이력.
// 그룹 제어 화면과 단말 드로어(개별 명령)가 같이 쓴다. docs/05 "5차 API · 명령", 사양서 §3.9.3 #3~#8, §3.10.7, §3.10.11.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ACK_STATUSES, AckStatus, CmdAct, CommandBody, CommandCreated, CommandDetail, CommandPreview,
  CommandSummary, CommandTargetRef, DurPreset, api, errorText,
} from "./api";
import { localTime, relTime, str } from "./format";
import { nf } from "./ui";

const POLL_MS = 3_000; // 결과 폴링(지시: 3초)
const HIST_MS = 10_000;
const MAX_ROWS = 300; // 결과 표에 한 번에 그리는 대상 수

export const ACT_LABEL: Record<CmdAct, string> = { off: "소등", on: "점등", pwm: "밝기", auto: "스케줄 복귀" };
export const CH_LABEL: Record<number, string> = { 1: "주등", 2: "입간판", 3: "PWM3" };
const UI_CH = [1, 2]; // 사양서 §3.10.7: 관리 화면은 1·2 만 보여 준다

type DurSel = DurPreset | "custom";
const DUR_OPTS: [DurSel, string][] = [
  ["30m", "30분"], ["1h", "1시간"], ["3h", "3시간"], ["tonight", "오늘 밤"], ["custom", "직접 입력"],
];
const MAX_MIN = 1440; // 최대 24시간(dur ≤ 86400)

export function durText(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "-";
  const m = Math.round(sec / 60);
  if (m < 60) return `${m}분`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return r ? `${h}시간 ${r}분` : `${h}시간`;
}

export function hms(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toTimeString().slice(0, 8);
}

/** "주등+입간판 소등 1시간" / "밝기 주등 70%, 입간판 40% 3시간" / "주등 스케줄 복귀" */
export function cmdText(c: { act: CmdAct; ch?: number[] | null; pwm?: number[] | null; dur?: number | null }, durLabel?: string): string {
  const ch = c.ch && c.ch.length ? c.ch : [1, 2, 3];
  const names = ch.map((k) => CH_LABEL[k] ?? `ch${k}`);
  const d = durLabel ?? (c.dur ? durText(c.dur) : "");
  if (c.act === "auto") return `${names.join("+")} 스케줄 복귀`;
  if (c.act === "pwm") return `밝기 ${ch.map((k, i) => `${CH_LABEL[k] ?? k} ${c.pwm?.[i] ?? "?"}%`).join(", ")} ${d}`.trim();
  return `${names.join("+")} ${ACT_LABEL[c.act] ?? c.act} ${d}`.trim();
}

/** 결과 상태 → [라벨, 색 클래스, 막대 색] */
export const ACK_VIEW: Record<AckStatus, [string, string, string]> = {
  OK: ["응답 OK", "b-ok", "var(--ok)"],
  LOCAL: ["현장 조작 중", "b-blue", "var(--use)"],
  EXPIRED: ["만료", "b-warn", "var(--warn)"],
  BAD: ["오류(형식)", "b-alarm", "var(--alarm)"],
  STATE: ["오류(승인 전)", "b-alarm", "var(--alarm)"],
  pending: ["응답 대기", "b-blue", "var(--line)"],
  NO_RESPONSE: ["무응답(실패)", "b-alarm", "var(--alarm)"],
  OFFLINE: ["오프라인 — 안 보냄", "b-off", "var(--off)"],
};

const cnt = (c: Partial<Record<AckStatus, number>>, k: AckStatus) => c[k] ?? 0;

// ---------------------------------------------------------------- 명령 폼

interface FormProps {
  target: CommandTargetRef | null;
  targetLabel: string;
  /** 보낼 수 없는 이유(권한·상태). 있으면 버튼을 막고 이유를 보인다. */
  blocked?: string | null;
  onSent: (c: CommandCreated) => void;
  /** 단말 하나일 때 채널별 설치 기준 밝기·지금 비율(ledBasis). 없으면(그룹) 비율 100%, 실제 출력 표시 없음. */
  basis?: LedBasis | null;
}

/** 채널 → 설정 항목(설치 기준 밝기). 사양서 UI 항목 명세 brightness. */
const MANUAL_KEY: Record<number, string> = { 1: "manual_40w", 2: "manual_5w1", 3: "manual_5w2" };

export interface LedBasis {
  /** 채널별 설치 기준 밝기 %(단말 설정 manual_*). 모르면 없음. */
  base: Record<number, number>;
  /** 밝기 슬라이더 처음 값 = 설치 기준 밝기 대비 %(원격 pwm). */
  ratio: Record<number, number>;
  /** 처음 값·기준의 출처 설명. */
  source: string;
}

/**
 * LED 제어 밝기 = **설치 기준 밝기에 곱하는 비율**(사양서 §3.10.7, 문제점 19번 단말측 회신).
 * 처음 값(문제점 12번 "서버가 마지막으로 안 밝기"): 채널이 **켜져 있을 때** 받은 Telemetry `pw`(실제 출력)가
 * 기준 밝기 뒤에 왔으면 pw ÷ 기준 = 지금 비율. 아니면 100%(= 설치 기준 그대로).
 * 꺼진 채널의 pw(0)는 밝기가 아니라 "꺼짐"이라 쓰지 않는다.
 */
export function ledBasis(
  tele: { on?: number; pw?: number[] } | null | undefined, teleAt: string | null | undefined,
  settings: { values: Record<string, number> | null; at: string | null } | null,
): LedBasis {
  const clamp = (n: number) => Math.max(0, Math.min(100, Math.round(Number(n) || 0)));
  const tAt = teleAt ? Date.parse(teleAt) : NaN;
  const sAt = settings?.at ? Date.parse(settings.at) : NaN;
  const v = settings?.values ?? null;
  const base: Record<number, number> = {};
  const ratio: Record<number, number> = {};
  const used = new Map<string, number>(); // 출처 → 시각
  for (const ch of [1, 2, 3]) {
    const sv = v?.[MANUAL_KEY[ch]];
    if (typeof sv === "number") base[ch] = clamp(sv);
    const tv = tele?.pw?.[ch - 1];
    const lit = typeof tv === "number" && tv > 0 && !Number.isNaN(tAt);
    const shown = UI_CH.includes(ch); // 출처 문구는 화면에 보이는 주등·입간판 것만
    if (lit && base[ch] > 0 && (Number.isNaN(sAt) || tAt >= sAt)) {
      ratio[ch] = clamp(((tv as number) / base[ch]) * 100);
      if (shown) used.set("점등 중 보고", tAt);
    } else {
      ratio[ch] = 100;
    }
    if (shown && base[ch] !== undefined && !Number.isNaN(sAt)) used.set("기준 = 단말 설정 동기화", sAt);
  }
  const when = (t: number) => new Date(t).toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  return { base, ratio, source: [...used].map(([s, t]) => `${s} ${when(t)}`).join(" · ") };
}

/** 단말 하나의 ledBasis — 설정 조회(기준 밝기)가 실패해도 비율 100% 로 연다. */
export function useLedBasis(
  uuid: string, tele: { on?: number; pw?: number[] } | null | undefined, teleAt: string | null | undefined,
): LedBasis {
  const [set, setSet] = useState<{ values: Record<string, number> | null; at: string | null } | null>(null);
  useEffect(() => {
    let live = true;
    api.getSettings(uuid)
      .then((s) => live && setSet({ values: s.values, at: s.last_result === "OK" && s.last_result_at && (!s.read_at || s.last_result_at > s.read_at) ? s.last_result_at : s.read_at }))
      .catch(() => live && setSet(null));
    return () => { live = false; };
  }, [uuid]);
  return useMemo(() => ledBasis(tele, teleAt, set), [tele, teleAt, set]);
}

const FULL: Record<number, number> = { 1: 100, 2: 100, 3: 100 };

/** 목업 #gBox — 명령(소등/점등/밝기/스케줄 복귀) · 채널 · 밝기 % · 유지시간 → "보내기 전 확인". */
export function CommandForm({ target, targetLabel, blocked, onSent, basis }: FormProps) {
  const [act, setAct] = useState<CmdAct>("off");
  const [ch, setCh] = useState<Record<number, boolean>>({ 1: true, 2: true });
  const [pwm, setPwm] = useState<Record<number, number>>(basis?.ratio ?? FULL);
  // 기준을 늦게 받아 오면(창을 연 뒤 설정 조회가 끝남) 그때 채운다. 사용자가 이미 움직였으면 두지 않는다.
  const [touched, setTouched] = useState(false);
  const ratio = basis?.ratio;
  useEffect(() => {
    if (ratio && !touched) setPwm(ratio);
  }, [ratio, touched]);
  const [dur, setDur] = useState<DurSel>("1h");
  const [custom, setCustom] = useState("90");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ body: CommandBody; res: CommandPreview } | null>(null);

  const chs = UI_CH.filter((k) => ch[k]);
  const customMin = Number(custom);
  const customOk = Number.isInteger(customMin) && customMin >= 1 && customMin <= MAX_MIN;
  const durLabel = dur === "custom" ? (customOk ? durText(customMin * 60) : "?") : DUR_OPTS.find((d) => d[0] === dur)![1];

  let invalid: string | null = null;
  if (!target) invalid = "대상을 고른다";
  else if (!chs.length) invalid = "채널을 하나 이상 고른다";
  else if (act !== "auto" && dur === "custom" && !customOk) invalid = `직접 입력은 1~${MAX_MIN}분`;
  const reason = blocked ?? invalid;

  function body(): CommandBody {
    const b: CommandBody = { target: target!, act, ch: chs };
    if (act === "pwm") b.pwm = chs.map((k) => pwm[k]);
    if (act !== "auto") {
      if (dur === "custom") b.dur = customMin * 60;
      else b.dur_preset = dur;
    }
    return b;
  }

  // 화면용 예상 payload(seq·ts 는 서버가 보낼 때 채운다)
  const shown: Record<string, unknown> = { type: "COMMAND", seq: "…", ts: "…", exp: 30, act, ch: chs };
  if (act === "pwm") shown.pwm = chs.map((k) => pwm[k]);
  if (act !== "auto") shown.dur = dur === "custom" ? (customOk ? customMin * 60 : "?") : dur === "tonight" ? "(서버 계산: 오늘 소등 시각까지)" : { "30m": 1800, "1h": 3600, "3h": 10800 }[dur];

  async function doPreview() {
    if (reason) return;
    setErr(null);
    setBusy(true);
    try {
      const b = body();
      const res = await api.previewCommand(b);
      setPreview({ body: b, res });
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="cmdform">
      <div className="ctl" role="group" aria-label="명령">
        {(["off", "on", "pwm", "auto"] as CmdAct[]).map((a) => (
          <button key={a} type="button" aria-pressed={act === a} onClick={() => setAct(a)}>{ACT_LABEL[a]}</button>
        ))}
      </div>
      <div className="chrow">
        <span className="cap">채널</span>
        {UI_CH.map((k) => (
          <label key={k} className="chk2">
            <input type="checkbox" checked={!!ch[k]} onChange={(e) => setCh((c) => ({ ...c, [k]: e.target.checked }))} />
            <b>{CH_LABEL[k]}</b> ({k})
          </label>
        ))}
      </div>
      {act === "pwm" && chs.map((k) => {
        const b = basis?.base[k];
        return (
          <div key={k} className="ctlrow">
            <span>{CH_LABEL[k]}</span>
            <input type="range" min={0} max={100} value={pwm[k]} aria-label={`${CH_LABEL[k]} 설치 기준 밝기 대비 %`}
              onChange={(e) => (setTouched(true), setPwm((p) => ({ ...p, [k]: Number(e.target.value) })))} />
            <b>{pwm[k]}%</b>
            <span className="cap out">{b !== undefined ? `실제 ${Math.round((b * pwm[k]) / 100)}%` : ""}</span>
          </div>
        );
      })}
      {act === "pwm" && (
        <div className="cap">
          설치 기준 밝기 대비 % — 단말이 설치 기준 밝기에 곱한다(사양서 §3.10.7). 100% = 설치 기준 그대로.
          {chs.some((k) => basis?.base[k] !== undefined) ? ` 실제 = 설치 기준(${chs.filter((k) => basis?.base[k] !== undefined).map((k) => `${CH_LABEL[k]} ${basis!.base[k]}%`).join(", ")}) × 비율.` : ""}
          {basis?.source ? ` (${basis.source})` : ""}
        </div>
      )}
      {act !== "auto" ? (
        <div className="dur">
          <span className="muted">유지</span>
          {DUR_OPTS.map(([k, l]) => (
            <button key={k} type="button" aria-pressed={dur === k} onClick={() => setDur(k)}>{l}</button>
          ))}
          {dur === "custom" && (
            <span className="bar2">
              <input type="number" min={1} max={MAX_MIN} step={1} value={custom} aria-label="유지시간(분)" onChange={(e) => setCustom(e.target.value)} />
              <span className="cap">분 (최대 {MAX_MIN} = 24시간){customOk ? ` = ${durText(customMin * 60)}` : ""}</span>
            </span>
          )}
        </div>
      ) : (
        <div className="cap" style={{ marginTop: 12 }}>스케줄 복귀(auto)는 유지시간 없이 고른 채널의 원격 명령을 즉시 해제한다(어느 경로로 왔든).</div>
      )}
      {dur === "tonight" && act !== "auto" && <div className="cap">오늘 밤 = 대상 좌표 기준 오늘 소등 시각까지 남은 초를 서버가 계산한다.</div>}
      <code className="payload">{JSON.stringify(shown)}</code>
      <button type="button" className="btn pri send" disabled={!!reason || busy} onClick={doPreview}>
        {reason ?? (busy ? "확인 중…" : `${targetLabel}에 ${cmdText({ act, ch: chs, pwm: chs.map((k) => pwm[k]) }, act === "auto" ? undefined : durLabel)} — 보내기 전 확인`)}
      </button>
      {err && <div className="err">{err}</div>}
      {preview && (
        <PreviewModal
          label={targetLabel}
          text={cmdText({ act: preview.body.act, ch: preview.body.ch, pwm: preview.body.pwm }, act === "auto" ? undefined : durLabel)}
          body={preview.body}
          res={preview.res}
          onCancel={() => setPreview(null)}
          onSent={(c) => {
            setPreview(null);
            onSent(c);
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 보내기 전 확인

function PreviewModal({ label, text, body, res, onCancel, onSent }: {
  label: string; text: string; body: CommandBody; res: CommandPreview;
  onCancel: () => void; onSent: (c: CommandCreated) => void;
}) {
  const isAll = body.target.kind === "all";
  const [step, setStep] = useState<1 | 2>(1);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const lights = body.act === "on" || body.act === "pwm";

  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && !busy && onCancel();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [busy, onCancel]);

  async function send() {
    setBusy(true);
    setErr(null);
    try {
      onSent(await api.createCommand(body));
    } catch (e) {
      setErr(errorText(e));
      setBusy(false);
    }
  }

  const tp = res.topics.length;
  return (
    <div className="modal-ov" onClick={() => !busy && onCancel()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="보내기 전 확인" onClick={(e) => e.stopPropagation()}>
        <div className="dh">
          <div><h3>보내기 전 확인</h3><div className="u">{label}</div></div>
          <button type="button" className="x" onClick={onCancel} disabled={busy} aria-label="닫기">✕</button>
        </div>
        <div className="db">
          <div className="sec">
            <h4>명령 <span>{text}</span></h4>
            <div className="grid2">
              <div className="met"><div className="l">대상 (운영 단말)</div><div className="v">{nf(res.expected)}대</div><div className="h">범위 안 승인(ACTIVE) 단말</div></div>
              <div className="met k"><div className="l">온라인 — 보냄</div><div className="v">{nf(res.online)}대</div><div className="h">이 수만큼 응답을 기다린다(최대 3분, 없으면 실패)</div></div>
              <div className={`met ${res.offline ? "o" : ""}`}><div className="l">오프라인 — 안 보냄</div><div className="v">{nf(res.offline)}대</div><div className="h">보내지도 기다리지도 않는다</div></div>
              <div className={`met ${lights && res.low_battery ? "a" : ""}`}><div className="l">저전압 (안 켜질 수)</div><div className="v">{nf(res.low_battery)}대</div><div className="h">{lights ? "BATT_LOW — 점등 명령이어도 켜지지 않는다" : "소등·복귀에는 영향 없음"}</div></div>
              <div className="met o"><div className="l">제외 (승인 안 됨)</div><div className="v">{nf(res.not_active)}대</div><div className="h">범위 안이지만 ACTIVE 가 아니라 보내지 않음</div></div>
              <div className="met"><div className="l">발행 topic</div><div className="v">{nf(tp)}개</div><div className="h">{res.dur !== null ? `유지 ${durText(res.dur)} (dur ${res.dur}초)` : "유지시간 없음(auto)"}</div></div>
            </div>
            <div className="topic">발행: <code>{res.topics[0] ?? "-"}</code>{tp > 1 ? ` 외 ${nf(tp - 1)}개 — 법정동마다 1회, seq 는 하나` : tp === 1 ? " 1회" : ""}</div>
            <code className="payload">{JSON.stringify(res.payload)}</code>
            <div className="cap" style={{ marginTop: 8 }}>seq 와 ts(보낸 시각)는 보낼 때 서버가 넣는다. 단말은 다음 송신 뒤 받을 수 있다(최대 약 5분).</div>
          </div>
          {res.expected === 0 && <div className="err">운영(ACTIVE) 단말이 없어 보낼 수 없다(NO_TARGETS).</div>}
          {res.expected > 0 && res.online === 0 && <div className="err">대상 단말이 모두 오프라인이라 보낼 수 없다(NO_ONLINE_TARGETS).</div>}
          {isAll && step === 2 ? (
            <div className="confirm">
              <b>전체 단말</b>에 <b>{text}</b>을 보낸다. 되돌리려면 다시 스케줄 복귀를 보내야 한다.
              <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="확인하려면 '전체'를 입력" aria-label="확인 문구" autoFocus />
              <div className="row">
                <button type="button" className="btn" onClick={onCancel} disabled={busy}>취소</button>
                <button type="button" className="btn pri" disabled={busy || typed.trim() !== "전체"} onClick={send}>{busy ? "보내는 중…" : "전체에 보내기"}</button>
              </div>
            </div>
          ) : (
            <div className="bar2">
              <span className="sp" />
              <button type="button" className="btn" onClick={onCancel} disabled={busy}>취소</button>
              <button type="button" className="btn pri" disabled={busy || res.expected === 0} onClick={() => (isAll ? setStep(2) : send())}>
                {busy ? "보내는 중…" : isAll ? "다음 — 한 번 더 확인" : `${nf(res.expected)}대에 보내기`}
              </button>
            </div>
          )}
          {err && <div className="err">{err}</div>}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 결과

/** 목업 #gProg — 발행함 시각 + COMMAND_ACK 집계 + 대상 표 + 개별 재시도. 3초 폴링, 끝나면 멈춘다. */
export function CommandResult({ seq, onClose, onDevice }: { seq: number; onClose?: () => void; onDevice?: (uuid: string) => void }) {
  const [c, setC] = useState<CommandDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<AckStatus | "">("");
  const [n, setN] = useState(0);

  const load = useCallback(async () => {
    try {
      const r = await api.getCommand(seq);
      setC(r);
      setErr(null);
      return r;
    } catch (e) {
      setErr(errorText(e));
      return null;
    }
  }, [seq]);

  useEffect(() => {
    setC(null);
    setMsg(null);
    setFilter("");
  }, [seq]);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const loop = async () => {
      const r = await load();
      if (!alive) return;
      if (!r || !r.finished_at) timer = setTimeout(loop, POLL_MS);
    };
    loop();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [load, n]);

  async function retry() {
    setBusy(true);
    setMsg(null);
    setErr(null);
    try {
      const r = await api.retryCommand(seq, null);
      setMsg(`개별 topic 으로 ${nf(r.resent)}대 다시 보냄 (같은 seq, 새 ts)`);
      setN((x) => x + 1);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  if (!c) {
    return (
      <div className="sec">
        <h4>명령 #{seq} 결과 <span>{onClose && <button type="button" className="btn sm" onClick={onClose}>닫기</button>}</span></h4>
        {err ? <div className="err">{err}</div> : <div className="muted">불러오는 중…</div>}
      </div>
    );
  }

  const k = c.counts;
  const errN = cnt(k, "BAD") + cnt(k, "STATE");
  const retryable = cnt(k, "pending") + cnt(k, "EXPIRED");
  const segs: [AckStatus, number][] = ACK_STATUSES.map((s) => [s, cnt(k, s)]);
  const rows = c.targets.filter((t) => !filter || t.status === filter);

  return (
    <div className="sec cmdres">
      <h4>
        명령 #{c.seq} 결과
        <span className="bar2">
          {c.finished_at ? <span className={`badge ${c.result === "OK" ? "b-ok" : c.result === "TIMEOUT" ? "b-alarm" : "b-warn"}`}>종료 {str(c.result)}</span>
            : <span className="badge b-blue">집계 중 · 3초마다</span>}
          {onClose && <button type="button" className="btn sm" onClick={onClose}>닫기</button>}
        </span>
      </h4>
      <div className="recv">
        <div>발행함 <b>{hms(c.sent_at)}</b> · {c.created_by} · {str(c.target_label)} · {cmdText(c)}</div>
        <div>단말 응답: {c.finished_at ? `끝남 ${hms(c.finished_at)}` : "COMMAND_ACK 수신 중"}</div>
      </div>
      <div className="pbar">
        {segs.map(([s, v]) => v > 0 && <i key={s} style={{ flexGrow: v, background: ACK_VIEW[s][2] }} title={`${ACK_VIEW[s][0]} ${v}`} />)}
      </div>
      <div className="plg">
        <span>응답 OK <b>{nf(cnt(k, "OK"))}</b></span>
        <span>현장 조작 중(LOCAL) <b>{nf(cnt(k, "LOCAL"))}</b></span>
        <span>만료(EXPIRED) <b>{nf(cnt(k, "EXPIRED"))}</b></span>
        <span>오류 <b>{nf(errN)}</b></span>
        <span>{c.finished_at ? "무응답" : "응답 대기"} <b>{nf(cnt(k, "pending"))}</b></span>
        <span>무응답(실패) <b>{nf(cnt(k, "NO_RESPONSE"))}</b></span>
        <span>오프라인 — 안 보냄 <b>{nf(cnt(k, "OFFLINE"))}</b></span>
        <span>보낸 대수 {nf(c.expected_count)}</span>
      </div>
      <div className="bar2">
        <button type="button" className="btn" disabled={busy || !!c.finished_at || retryable === 0} onClick={retry}
          title="무응답(pending)·만료(EXPIRED) 대상만 개별 topic 으로 같은 seq 재발송">
          무응답·만료 개별 재시도 ({nf(retryable)})
        </button>
        <span className="cap">3분 안에 응답이 없으면 무응답(실패)으로 끝난다. 그 안에는 단말이 보낸 직후 서버가 자동 재시도(최대 3회). 오프라인 단말은 보내지 않는다.</span>
      </div>
      {msg && <div className="okl">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <div className="tabs">
        <button type="button" aria-pressed={filter === ""} onClick={() => setFilter("")}>전체<b>{nf(c.targets.length)}</b></button>
        {ACK_STATUSES.map((s) => cnt(k, s) > 0 && (
          <button key={s} type="button" aria-pressed={filter === s} onClick={() => setFilter(s)}>{ACK_VIEW[s][0]}<b>{nf(cnt(k, s))}</b></button>
        ))}
      </div>
      <div style={{ overflowX: "auto", maxHeight: 360 }}>
        <table className="mini">
          <thead>
            <tr><th>상태</th><th>시설명</th><th>UUID</th><th>지역</th><th className="n">시도</th><th>마지막 발송</th><th>응답</th><th>통신</th></tr>
          </thead>
          <tbody>
            {rows.slice(0, MAX_ROWS).map((t) => (
              <tr key={t.uuid} style={onDevice ? { cursor: "pointer" } : undefined} onClick={() => onDevice?.(t.uuid)}>
                <td><span className={`badge ${ACK_VIEW[t.status]?.[1] ?? "b-off"}`}>{ACK_VIEW[t.status]?.[0] ?? t.status}</span></td>
                <td>{str(t.site)}</td>
                <td className="mono">{t.uuid}</td>
                <td>{str(t.node_name)}</td>
                <td className="n">{t.attempts}</td>
                <td>{hms(t.last_sent_at)}</td>
                <td>{hms(t.acked_at)}</td>
                <td><span className="dot" style={{ background: t.is_online ? "var(--ok)" : "var(--off)" }} /> {t.is_online ? "온라인" : "오프라인"}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={8} className="muted">없음</td></tr>}
          </tbody>
        </table>
        {rows.length > MAX_ROWS && <div className="cap">처음 {MAX_ROWS}대만 표시 — 상태 탭으로 좁혀 본다.</div>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 이력

/** 명령 이력 — 누가·언제·무엇을·대상·결과(§3.9.3 #8). 행 클릭 → onOpen(seq). */
export function CommandHistory({ uuid, nodeId, tick, selected, onOpen, limit = 50 }: {
  uuid?: string; nodeId?: number; tick?: number; selected?: number | null;
  onOpen: (seq: number) => void; limit?: number;
}) {
  const [rows, setRows] = useState<CommandSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .listCommands({ limit, uuid, node_id: nodeId })
        .then((r) => alive && (setRows(r), setErr(null)))
        .catch((e) => alive && setErr(errorText(e)));
    load();
    const id = setInterval(load, HIST_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [uuid, nodeId, tick, limit]);

  return (
    <>
      {err && <div className="err">{err}</div>}
      <div style={{ overflowX: "auto" }}>
        <table className="mini">
          <thead>
            <tr><th>#</th><th>시각</th><th>누가</th><th>대상</th><th>명령</th><th>결과</th></tr>
          </thead>
          <tbody>
            {(rows ?? []).map((c) => {
              const k = c.counts;
              const errN = cnt(k, "BAD") + cnt(k, "STATE");
              return (
                <tr key={c.seq} style={{ cursor: "pointer" }} className={selected === c.seq ? "sel" : ""} onClick={() => onOpen(c.seq)}>
                  <td className="muted">#{c.seq}</td>
                  <td title={localTime(c.sent_at)}>{localTime(c.sent_at)} <span className="md">{relTime(c.sent_at)}</span></td>
                  <td>{c.created_by}</td>
                  <td title={`${c.target_kind} ${str(c.target_id)}`}>{c.target_kind === "all" ? "전체" : str(c.target_label)}</td>
                  <td>{cmdText(c)}</td>
                  <td>
                    <span className="c-ok">OK {nf(cnt(k, "OK"))}</span>
                    {cnt(k, "LOCAL") > 0 && <span className="md">LOCAL {nf(cnt(k, "LOCAL"))}</span>}
                    {cnt(k, "EXPIRED") > 0 && <span className="md c-warn">만료 {nf(cnt(k, "EXPIRED"))}</span>}
                    {errN > 0 && <span className="md c-alarm">오류 {nf(errN)}</span>}
                    {cnt(k, "pending") > 0 && <span className="md">응답 대기 {nf(cnt(k, "pending"))}</span>}
                    {cnt(k, "NO_RESPONSE") > 0 && <span className="md c-alarm">무응답 {nf(cnt(k, "NO_RESPONSE"))}</span>}
                    {cnt(k, "OFFLINE") > 0 && <span className="md">오프라인 {nf(cnt(k, "OFFLINE"))}</span>}
                    <span className="md">/ {nf(c.expected_count)}</span>
                    {c.finished_at ? <span className="md">· {str(c.result)}</span> : <span className="md c-blue">· 진행 중</span>}
                  </td>
                </tr>
              );
            })}
            {rows && rows.length === 0 && <tr><td colSpan={6} className="muted">명령 이력이 없습니다.</td></tr>}
            {!rows && !err && <tr><td colSpan={6} className="muted">불러오는 중…</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
