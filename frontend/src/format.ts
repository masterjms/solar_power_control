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
  [0x0004, "LED_FAULT"], // 2026-09-28-2 부터 PWM 1·2·3 전체 하나(§16.2.2). 0x0008 은 예약
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

/** 이벤트 payload 를 kind 별로 사람이 읽을 요약 한 줄로. 모르는 kind 는 JSON 그대로. */
export function eventSummary(kind: string, payload: unknown): string {
  const p = (payload && typeof payload === "object" ? payload : {}) as Record<string, unknown>;
  const pick = (keys: string[]) =>
    keys
      .filter((k) => p[k] !== undefined && p[k] !== null)
      .map((k) => `${k}=${typeof p[k] === "object" ? JSON.stringify(p[k]) : String(p[k])}`)
      .join(" ");
  switch (kind) {
    case "REGISTER_ACK": {
      const s = pick(["state", "site", "reason", "grp", "published"]);
      return `단말에 발행 ${s}`.trim();
    }
    case "STATE_CHANGE": {
      const from = p.from ?? p.old_state ?? p.prev;
      const to = p.to ?? p.new_state ?? p.state;
      const base = from !== undefined || to !== undefined ? `${str(from)} → ${str(to)}` : "";
      return `${base} ${pick(["site", "reason"])}`.trim() || JSON.stringify(payload);
    }
    case "CONFIG_SET":
      return `서버→단말 ${pick(["cv", "ti", "ka", "lat", "lon", "published", "reason"])}`.trim();
    case "CONFIG_ACK":
      return `단말→서버 ${pick(["cv", "result", "ok", "err", "ti", "ka"])}`.trim() || JSON.stringify(payload);
    case "ONLINE":
    case "OFFLINE":
      return `${kind} ${pick(["source", "reason", "at", "ts"])}`.trim();
    case "REGISTER":
      return pick(["fw", "cv", "ti", "ka", "ss", "device_model"]) || JSON.stringify(payload);
    case "PONG":
      return pick(["seq", "ts"]) || JSON.stringify(payload);
    case "LOST":
    case "REBOOT":
      return pick(["sq", "last_sq", "prev_sq", "gap"]) || JSON.stringify(payload);
    default:
      return JSON.stringify(payload);
  }
}

/** 배터리 전압 x100 → 소수 1자리 "24.5 V"(문제점 #11). */
export function volt1(v: number | null | undefined): string {
  return v === null || v === undefined ? "" : `${(v / 100).toFixed(1)} V`;
}

/** 전력 x100 → 소수 1자리 W. 값이 없으면 공백(문제점 #11 "정보가 없으면 공백"). */
export function watt1(v: number | null | undefined): string {
  return v === null || v === undefined ? "" : `${(v / 100).toFixed(1)} W`;
}

/** 에너지 Wh → 소수 1자리. 1 kWh 넘으면 kWh. */
export function wh1(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return v >= 1000 ? `${(v / 1000).toFixed(1)} kWh` : `${v.toFixed(1)} Wh`;
}

/** 온실가스 g → gCO2eq / kgCO2eq / MgCO2eq(톤) 자동 단위. */
export function co2Text(g: number | null | undefined): string {
  if (g === null || g === undefined) return "";
  if (g >= 1e6) return `${(g / 1e6).toFixed(2)} MgCO2eq`;
  if (g >= 1e3) return `${(g / 1e3).toFixed(1)} kgCO2eq`;
  return `${g.toFixed(1)} gCO2eq`;
}
