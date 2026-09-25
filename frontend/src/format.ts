// 표시용 변환. 사양서의 단위(x100)와 비트 해석.

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}초 전`;
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  return `${Math.floor(s / 86400)}일 전`;
}

export function localTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("ko-KR", { hour12: false });
}

/** x100 정수 → 소수 2자리 문자열. null 이면 "-" */
export function div100(v: number | null | undefined, unit = "", sign = false): string {
  if (v === null || v === undefined) return "-";
  const n = v / 100;
  const s = sign && n > 0 ? `+${n.toFixed(2)}` : n.toFixed(2);
  return unit ? `${s} ${unit}` : s;
}

export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "-" : `${v} %`;
}

export function mdLabel(md: number | null | undefined): string {
  switch (md) {
    case 0: return "0 스케줄";
    case 1: return "1 현장수동";
    case 2: return "2 원격";
    case null:
    case undefined: return "-";
    default: return String(md);
  }
}

const ER_FLAGS: [number, string][] = [
  [0x0001, "BATT_LOW"],
  [0x0002, "RTC_INVALID"],
  [0x0004, "LED_FAULT_M"],
  [0x0008, "LED_FAULT_S"],
  [0x0010, "MPPT_OFFLINE"],
  [0x0020, "SCHEDULE_BAD"],
  [0x0040, "LTE_OFFLINE"],
];

export function erLabel(er: number | null | undefined): string {
  if (er === null || er === undefined) return "-";
  if (er === 0) return "0 (정상)";
  const names = ER_FLAGS.filter(([bit]) => er & bit).map(([, n]) => n);
  const known = ER_FLAGS.reduce((a, [bit]) => a | bit, 0);
  if (er & ~known) names.push(`unknown:0x${(er & ~known).toString(16)}`);
  return `0x${er.toString(16).padStart(4, "0")} ${names.join("|")}`;
}

export function hex(v: number | null | undefined): string {
  return v === null || v === undefined ? "-" : `0x${v.toString(16).padStart(4, "0")} (${v})`;
}

export function str(v: unknown): string {
  if (v === null || v === undefined || v === "") return "-";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}
