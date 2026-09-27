// 목업(docs/spec/ui/solar_light_dashboard_v15.html)의 공통 조각. 카드·배지·자리표시.
import { ReactNode } from "react";
import { DeviceState } from "./api";

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

const STATE_LABEL: Record<DeviceState, [string, string]> = {
  PENDING: ["승인 대기", "b-blue"],
  ACTIVE: ["운영", "b-ok"],
  SUSPENDED: ["일시 중지", "b-warn"],
  REJECTED: ["거부", "b-alarm"],
  RETIRED: ["폐기", "b-off"],
};

export function stateLabel(s: DeviceState): string {
  return STATE_LABEL[s]?.[0] ?? s;
}

/** 승인 상태 배지. 목업의 .badge.b-* 색을 state 에 대응. */
export function StateBadge({ state }: { state: DeviceState }) {
  const [label, cls] = STATE_LABEL[state] ?? [state, "b-off"];
  return <span className={`badge ${cls}`} title={state}>{label} <span style={{ fontWeight: 400, marginLeft: 4 }}>{state}</span></span>;
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

/** 점등 표시 — 목업의 .bulb */
export function Lamp({ on }: { on: number | null | undefined }) {
  if (on === null || on === undefined) return <><span className="bulb" /> <span className="muted">알 수 없음</span></>;
  return on ? <><span className="bulb on" /> 점등</> : <><span className="bulb" /> 소등</>;
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
