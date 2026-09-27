// 단말 운전 설정(S-23) 화면 계산. 항목 정의는 전부 GET /api/settings/schema(= ui_items.json)에서 온다 —
// 여기에는 키 이름을 적지 않는다. 규칙은 schema.rules 의 id/text 로, 계산식은 UI_항목_명세 §2.2 로.
import { SettingsItem, SettingsSchema } from "./api";

export const allItems = (s: SettingsSchema): SettingsItem[] => s.groups.flatMap((g) => g.items);

/** scale 의 자릿수(100 → 2). */
const decimals = (scale: number) => (scale > 1 ? Math.round(Math.log10(scale)) : 0);

/** 단말 정수 → 화면 글자(값 / scale). */
export function toText(it: SettingsItem, v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return it.scale > 1 ? (v / it.scale).toFixed(decimals(it.scale)) : String(v);
}

/** 화면 글자 → 단말 정수. 비었거나 숫자가 아니면 null. */
export function parseText(it: SettingsItem, t: string | undefined): number | null {
  if (t === undefined || t.trim() === "") return null;
  const n = Number(t.trim());
  if (!Number.isFinite(n)) return null;
  return Math.round(n * (it.scale || 1));
}

/** 표시용 "값 단위". */
export function fmtVal(it: SettingsItem | undefined, v: number | string | null | undefined): string {
  if (v === null || v === undefined || v === "") return "-";
  if (!it || typeof v !== "number") return String(v);
  return `${toText(it, v)}${it.unit ? ` ${it.unit}` : ""}`;
}

/** §2.2 실제 출력 = round(기준 x 시작 / 100), 99 초과 99, 0 초과 10 미만 10. */
export function effPwm(base: number, mult: number): number {
  const r = Math.round((base * mult) / 100);
  if (r > 99) return 99;
  if (r > 0 && r < 10) return 10;
  return r;
}

export interface RuleError {
  id: string;
  keys: string[];
  text: string;
}

/** 다단계 시각 키(`<prefix><n>_h`, `<prefix><n>_m`)를 schema 에서 찾는다. 단계 번호순. */
export function stageKeys(s: SettingsSchema): { n: number; h: string; m: string }[] {
  const keys = new Set(allItems(s).map((i) => i.key));
  const out: { n: number; h: string; m: string }[] = [];
  allItems(s).forEach((i) => {
    const mt = /^(.*?)(\d+)_h$/.exec(i.key);
    if (i.widget === "time_h" && mt && keys.has(`${mt[1]}${mt[2]}_m`)) out.push({ n: Number(mt[2]), h: i.key, m: `${mt[1]}${mt[2]}_m` });
  });
  return out.sort((a, b) => a.n - b.n);
}

/**
 * §2.1 화면에서 미리 막는 규칙.
 * - "a < b" 꼴 text 는 그대로 비교(배터리 cut<rtn).
 * - stage_order: 1→2→3→4 시각이 앞으로만 가고(자정 넘김 허용, 같은 시각 불가), 1단계부터 4단계까지 24시간 미만.
 *   점등 시각은 날마다 달라 화면은 1→4 구간만 본다. 최종 판정은 서버(SETTINGS_RULE)·단말(RULE).
 */
export function checkRules(s: SettingsSchema, v: Record<string, number | null>): RuleError[] {
  const errs: RuleError[] = [];
  const labelOf = (k: string) => allItems(s).find((i) => i.key === k)?.label ?? k;
  for (const r of s.rules) {
    const lt = /^\s*(\w+)\s*<\s*(\w+)\s*$/.exec(r.text);
    if (lt) {
      const [a, b] = [lt[1], lt[2]];
      const va = v[a], vb = v[b];
      if (va === null || va === undefined || vb === null || vb === undefined) continue;
      if (!(va < vb)) errs.push({ id: r.id, keys: [a, b], text: `${labelOf(a)}은(는) ${labelOf(b)}보다 낮아야 한다. ${r.why}` });
      continue;
    }
    if (r.id === "stage_order") {
      const st = stageKeys(s);
      const t = st.map((x) => {
        const h = v[x.h], m = v[x.m];
        return h === null || h === undefined || m === null || m === undefined ? null : h * 60 + m;
      });
      if (t.some((x) => x === null) || t.length < 2) continue;
      let sum = 0;
      let bad = false;
      for (let i = 0; i + 1 < t.length; i++) {
        const d = ((t[i + 1]! - t[i]!) % 1440 + 1440) % 1440;
        if (d === 0) bad = true;
        sum += d;
      }
      if (bad || sum >= 1440) {
        errs.push({ id: r.id, keys: st.flatMap((x) => [x.h, x.m]), text: `다단계 시각은 1→2→3→4단계 순서로 이어지고(자정 넘김 허용) 24시간 안이어야 한다. ${r.why}` });
      }
    }
  }
  return errs;
}

export function rangeError(it: SettingsItem, v: number | null): string | null {
  if (v === null) return "값이 없다";
  if (v < it.min || v > it.max) return `범위 ${toText(it, it.min)} ~ ${toText(it, it.max)}${it.unit ? ` ${it.unit}` : ""}`;
  return null;
}

export const utf8Bytes = (s: string) => new TextEncoder().encode(s).length;

/** 표 조건 지역 이름 검사(docs/05: UTF-8 47바이트, `"` `\` 제어문자 불가). */
export function regionError(s: string, maxBytes: number): string | null {
  if (!s.trim()) return "지역 이름을 넣는다";
  if (utf8Bytes(s) > maxBytes) return `${maxBytes}바이트를 넘는다(한글 약 ${Math.floor(maxBytes / 3)}자)`;
  // eslint-disable-next-line no-control-regex
  if (/["\\\u0000-\u001f\u007f]/.test(s)) return "따옴표·역슬래시·제어문자는 쓸 수 없다";
  return null;
}

export const TBL_SRC: Record<number, string> = { 0: "펌웨어 기본 표", 1: "PC 도구", 2: "서버" };

export const signed = (n: number) => (n > 0 ? `+${n}` : String(n));

/** 12.5 → "12시간 30분" */
export function hoursText(h: number): string {
  const hh = Math.floor(h);
  const mm = Math.round((h - hh) * 60);
  return mm ? `${hh}시간 ${mm}분` : `${hh}시간`;
}
