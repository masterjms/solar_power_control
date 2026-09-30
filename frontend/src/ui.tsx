// 목업(docs/spec/ui/solar_light_dashboard_v15.html)의 공통 조각. 카드·배지·자리표시.
import { ReactNode } from "react";
import { DeviceState, SettingsSync } from "./api";

/** section.card — 목업의 카드 틀. h500 등 크기 클래스는 className 으로. */
export function Card({
  title, meta, className = "", children,
}: { title: ReactNode; meta?: ReactNode; className?: string; children?: ReactNode }) {
  return (
    <section className={`card ${className}`.trim()}>
      <div className="ch">
        <h2>{title}</h2>
        {meta !== undefined && <div className="meta">{meta}</div>}
      </div>
      <div className="cb">{children}</div>
    </section>
  );
}

/** 아직 백엔드 API 가 없는 기능의 자리. 목업과 같은 크기·위치에 제목과 "준비 중 (N차)" 만. */
export function Placeholder({ stage, note }: { stage: string; note?: string }) {
  return (
    <div className="ph">
      <b>준비 중 ({stage})</b>
      {note && <span>{note}</span>}
    </div>
  );
}

export function PlaceholderCard({
  title, meta, stage, note, className,
}: { title: ReactNode; meta?: ReactNode; stage: string; note?: string; className?: string }) {
  return (
    <Card title={title} meta={meta} className={className}>
      <Placeholder stage={stage} note={note} />
    </Card>
  );
}

/** 상태 문구는 짧게 — 운영·대기·중지·거부·폐기(문제점 #11). 영문 state 는 마우스를 올리면. */
const STATE_LABEL: Record<DeviceState, [string, string]> = {
  PENDING: ["대기", "b-blue"],
  ACTIVE: ["운영", "b-ok"],
  SUSPENDED: ["중지", "b-warn"],
  REJECTED: ["거부", "b-alarm"],
  RETIRED: ["폐기", "b-off"],
};

export function stateLabel(s: DeviceState): string {
  return STATE_LABEL[s]?.[0] ?? s;
}

/** 승인 상태 배지. 목업의 .badge.b-* 색을 state 에 대응. */
export function StateBadge({ state }: { state: DeviceState }) {
  const [label, cls] = STATE_LABEL[state] ?? [state, "b-off"];
  return <span className={`badge ${cls}`} title={state}>{label}</span>;
}

/** 온라인 표시 — 목업의 통신 열처럼 점 + 글자. */
export function OnlineMark({ on, title }: { on: boolean; title?: string }) {
  return (
    <span title={title} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span className="dot" style={{ background: on ? "var(--ok)" : "var(--off)" }} />
      {on ? "온라인" : "오프라인"}
    </span>
  );
}

/** 배터리 잔량 막대 + %. 목업의 .bt */
export function Battery({ sc }: { sc: number | null | undefined }) {
  if (sc === null || sc === undefined) return <span className="muted">-</span>;
  const c = sc < 20 ? "var(--alarm)" : sc < 30 ? "var(--warn)" : "var(--ok)";
  return (
    <>
      <span className="bt"><i style={{ width: `${Math.max(0, Math.min(100, sc))}%`, background: c }} /></span>
      {sc}%
    </>
  );
}

const OFF_TITLE = "통신 두절 — 마지막 보고와 관계없이 소등으로 본다(문제점 15번)";

/** 점등 표시 — 목업의 .bulb. online=false(통신 두절)면 소등(문제점 15번). */
export function Lamp({ on, online }: { on: number | null | undefined; online?: boolean }) {
  if (online === false) return <span title={OFF_TITLE}><span className="bulb" /> 소등 <span className="muted">(두절)</span></span>;
  if (on === null || on === undefined) return <><span className="bulb" /> <span className="muted">알 수 없음</span></>;
  return on ? <><span className="bulb on" /> 점등</> : <><span className="bulb" /> 소등</>;
}

/** 채널 점등 한 칸 — "주등 ● 점등". pw 가 없으면 공백. */
export function ChLamp({ label, on }: { label: string; on: boolean | null }) {
  if (on === null) return <span className="chl"><small>{label}</small></span>;
  return <span className="chl"><small>{label}</small><span className={`bulb ${on ? "on" : ""}`} />{on ? "점등" : "소등"}</span>;
}

/** 주등(PWM1)·입간판(PWM2) 점등 — Telemetry pw 배열(%)이 0 보다 크면 점등. pw 가 없으면 on 으로 주등만.
 *  online=false(통신 두절)면 마지막 보고가 점등이어도 둘 다 소등(문제점 15번). */
export function LampPair({ t, online }: { t: { on?: number; pw?: number[] } | null | undefined; online?: boolean }) {
  if (online === false)
    return <span className="chp" title={OFF_TITLE}><ChLamp label="주등" on={false} /><ChLamp label="입간판" on={false} /></span>;
  const pw = t?.pw;
  const main = pw && pw.length > 0 ? pw[0] > 0 : t?.on === undefined || t?.on === null ? null : t.on === 1;
  const sign = pw && pw.length > 1 ? pw[1] > 0 : null;
  return <span className="chp"><ChLamp label="주등" on={main} /><ChLamp label="입간판" on={sign} /></span>;
}

export function Met({ l, v, h, cls }: { l: ReactNode; v: ReactNode; h?: ReactNode; cls?: string }) {
  return (
    <div className={`met ${cls ?? ""}`.trim()}>
      <div className="l">{l}</div>
      <div className="v">{v}</div>
      {h !== undefined && <div className="h">{h}</div>}
    </div>
  );
}

export const nf = (n: number | null | undefined) => (n === null || n === undefined ? "-" : n.toLocaleString("ko-KR"));

const SITE_MAX = 24; // 백엔드 site 한도(docs/05)
const SITE_HINT = 16; // 사양서 §3.9.3 #2 — 단말 OLED 한글 16자

/** 시설명 길이 안내 — 24자까지 받지만 단말 화면은 한글 16자. */
export function SiteHint({ value }: { value: string }) {
  const n = [...value].length;
  return (
    <small className={n > SITE_HINT ? "c-warn" : ""}>
      {n}/{SITE_HINT}자 권장 (단말 OLED 한글 {SITE_HINT}자, 최대 {SITE_MAX})
    </small>
  );
}

const SYNC_LABEL: Record<SettingsSync, [string, string, string]> = {
  unknown: ["읽지 않음", "b-off", "서버가 아직 단말 운전 설정을 모른다 — 단말에서 읽기"],
  synced: ["동기", "b-ok", "DB 값 = 단말 값(sh 일치)"],
  writing: ["쓰는 중", "b-blue", "SETTINGS_SET 보냄, SETTINGS_ACK 대기"],
  local_saved: ["현장에서 저장함", "b-warn", "Telemetry ss 가 바뀜 — 현장 PC 도구·OLED 로 저장했다. 다시 읽어 확인"],
  device_changed: ["단말 값이 바뀜", "b-alarm", "읽어 보니 단말 sh 가 DB 와 다름 — 받아들이기 또는 되돌리기"],
};

export function syncLabel(s: SettingsSync | null | undefined): string {
  return SYNC_LABEL[s ?? "unknown"]?.[0] ?? String(s);
}

/** 운전 설정 동기 배지(S-23, device_settings.sync). */
export function SyncBadge({ sync }: { sync: SettingsSync | null | undefined }) {
  const s = sync ?? "unknown";
  const [label, cls, why] = SYNC_LABEL[s] ?? [s, "b-off", ""];
  return <span className={`badge ${cls}`} title={`${s} — ${why}`}>{label}</span>;
}
