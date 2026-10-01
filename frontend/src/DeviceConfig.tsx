// 단말 설정(#config, S-23) — 왼쪽 단말 탐색기(지역별로 접고 펴기), 오른쪽 선택 단말의 운전 설정.
// 항목·범위·배율·도움말은 GET /api/settings/schema(= ui_items.json)에서만 온다. 흐름은 "읽고 나서 쓴다"(ADR-007, 명세 §8.5).
import { CitySelect } from "./CitySelect";
import { Fragment, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, ApiErrorException, Device, DeviceEvent, DeviceSettings, SchedulePreview, SettingsHistoryRow, SettingsItem,
  SettingsSchema, SettingsSent, SettingsSync, SettingsTbl, SettingsTblIn, errorText,
} from "./api";
import { eventSummary, localTime, relTime, str } from "./format";
import { Card, Met, OnlineMark, StateBadge, SyncBadge, nf, syncLabel } from "./ui";
import {
  RuleError, TBL_SRC, allItems, checkRules, effPwm, fmtVal, hoursText, parseText, rangeError, regionError,
  signed, sliderStep, stageKeys, toText, utf8Bytes,
} from "./settingsLogic";

const REFRESH_MS = 10_000;
const POLL_MS = 3_000; // 요청이 대기 중이거나 writing 이면
const LIST_SIZE = 500;

// ---------------- 스키마(한 번만 읽는다) ----------------
let schemaCache: SettingsSchema | null = null;
export function useSchema() {
  const [schema, setSchema] = useState<SettingsSchema | null>(schemaCache);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (schemaCache) return;
    api.settingsSchema().then((s) => ((schemaCache = s), setSchema(s))).catch((e) => setError(errorText(e)));
  }, []);
  return { schema, error };
}

function uuidFromHash(): string | null {
  const parts = location.hash.replace(/^#/, "").split("/");
  return parts[0] === "config" && parts[1] ? parts[1].toUpperCase() : null;
}

const SYNC_CLS: Record<SettingsSync, string> = {
  unknown: "c-off", synced: "c-ok", writing: "c-blue", local_saved: "c-warn", device_changed: "c-alarm",
};

// ---------------- 왼쪽: 단말 탐색기 ----------------
type ScopeFilter = "rw" | "ACTIVE" | "PENDING" | "";

function DevicePicker({ selected, tick }: { selected: string | null; tick: number }) {
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [scope, setScope] = useState<ScopeFilter>("rw");
  const [attention, setAttention] = useState(false);
  const [items, setItems] = useState<Device[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [closed, setClosed] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const id = setTimeout(() => setQ(text.trim()), 300);
    return () => clearTimeout(id);
  }, [text]);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .listDevices({ page: 1, size: LIST_SIZE, q: q || undefined, state: scope === "ACTIVE" || scope === "PENDING" ? scope : undefined })
        .then((r) => alive && (setItems(r.items), setTotal(r.total), setError(null)))
        .catch((e) => alive && setError(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [q, scope, tick]);

  const groups = useMemo(() => {
    const m = new Map<string, Device[]>();
    (items ?? [])
      .filter((d) => (scope === "rw" ? d.state === "ACTIVE" || d.state === "PENDING" : true))
      .filter((d) => (attention ? d.settings_sync === "local_saved" || d.settings_sync === "device_changed" : true))
      .forEach((d) => {
        const k = d.node_path ?? "(지역 미배정)";
        if (!m.has(k)) m.set(k, []);
        m.get(k)!.push(d);
      });
    return [...m.entries()]
      .sort((a, b) => (a[0].startsWith("(") ? 1 : b[0].startsWith("(") ? -1 : a[0].localeCompare(b[0], "ko")))
      .map(([path, ds]) => ({ path, ds: ds.sort((x, y) => (x.site ?? x.uuid).localeCompare(y.site ?? y.uuid, "ko")) }));
  }, [items, scope, attention]);

  const toggle = (k: string) =>
    setClosed((s) => {
      const n = new Set(s);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });
  const pick = (u: string) => {
    location.hash = `#config/${u}`;
  };

  const shown = groups.reduce((a, g) => a + g.ds.length, 0);
  return (
    <Card title="단말" meta={items ? `${nf(shown)}대${total > LIST_SIZE ? ` · ${nf(LIST_SIZE)}대까지만 — 검색으로 좁히기` : ""}` : ""}>
      <input type="search" placeholder="시설명 / UUID 검색" aria-label="단말 검색" value={text} onChange={(e) => setText(e.target.value)} />
      <div className="bar2">
        <select value={scope} aria-label="승인 상태" onChange={(e) => setScope(e.target.value as ScopeFilter)}>
          <option value="rw">읽기 가능 (승인 대기·운영)</option>
          <option value="ACTIVE">운영 ACTIVE (쓰기 가능)</option>
          <option value="PENDING">승인 대기 PENDING</option>
          <option value="">전체</option>
        </select>
        <label className="chk2" title="settings_sync 가 local_saved / device_changed 인 단말만">
          <input type="checkbox" checked={attention} onChange={(e) => setAttention(e.target.checked)} />확인 필요만
        </label>
      </div>
      {error && <div className="err">{error}</div>}
      <div className="tree tall" role="tree" aria-label="단말 탐색기">
        {groups.map((g) => {
          const open = !closed.has(g.path) || !!q;
          const warn = g.ds.filter((d) => d.settings_sync === "local_saved" || d.settings_sync === "device_changed").length;
          return (
            <Fragment key={g.path}>
              <div className="tn" role="treeitem" aria-expanded={open} tabIndex={0} title={g.path}
                onClick={() => toggle(g.path)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), toggle(g.path))}>
                <button type="button" className="tc" tabIndex={-1} aria-label={open ? "접기" : "펼치기"}>{open ? "▾" : "▸"}</button>
                <span className="tnm">{g.path.split(">").map((x) => x.trim()).slice(-2).join(" > ")}</span>
                <span className="tct">{nf(g.ds.length)}대{warn > 0 && <em className="c-warn">확인 {warn}</em>}</span>
              </div>
              {open && g.ds.map((d) => {
                const s = d.settings_sync ?? "unknown";
                return (
                  <div key={d.uuid} className={`tn ${selected === d.uuid ? "on" : ""}`} role="treeitem" aria-selected={selected === d.uuid}
                    tabIndex={0} style={{ paddingLeft: 22 }} title={`${d.uuid} · ${d.state} · 설정 ${syncLabel(s)}`}
                    onClick={() => pick(d.uuid)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), pick(d.uuid))}>
                    <span className="dot" style={{ background: d.is_online ? "var(--ok)" : "var(--off)", margin: "0 6px" }} />
                    <span className="tnm">{d.site ?? "(시설명 없음)"}<small>{d.uuid.slice(-8)}</small></span>
                    <span className={`tct ${SYNC_CLS[s]}`}>{d.state === "ACTIVE" ? syncLabel(s) : `${d.state === "PENDING" ? "승인 대기" : d.state} · ${syncLabel(s)}`}</span>
                  </div>
                );
              })}
            </Fragment>
          );
        })}
        {items && shown === 0 && <div className="tempty">조건에 맞는 단말이 없습니다.</div>}
        {!items && !error && <div className="tempty">불러오는 중…</div>}
      </div>
    </Card>
  );
}

// ---------------- 입력 칸 ----------------
export interface FieldCtx {
  schema: SettingsSchema;
  edit: Record<string, string>;
  values: Record<string, number> | null; // null = 읽지 않음(기본값을 흐리게 참고로만)
  disabled: boolean;
  bad: Set<string>;
  serverErr: Record<string, string>;
  set: (k: string, t: string) => void;
}

function shownText(c: FieldCtx, it: SettingsItem): string {
  return c.values ? c.edit[it.key] ?? "" : toText(it, it.default);
}
function isDirty(c: FieldCtx, it: SettingsItem): boolean {
  return !!c.values && parseText(it, c.edit[it.key]) !== c.values[it.key];
}

function NumInput({ c, it }: { c: FieldCtx; it: SettingsItem }) {
  // 전압(x100)은 0.1 V 씩 — 화면은 소수 1자리(문제점 #2). 단말 값이 0.01 단위면 그 값 그대로 보인다.
  const step = it.scale > 1 ? sliderStep(it) / it.scale : 1;
  return (
    <input className="num" type="number" step={step} min={it.min / (it.scale || 1)} max={it.max / (it.scale || 1)}
      aria-label={it.label} disabled={c.disabled}
      value={shownText(c, it)} onChange={(e) => c.set(it.key, e.target.value)} />
  );
}

/** 선택바 — 모든 항목에 같은 자리(문제점 #2 "항목이름 선택바 숫자 단위"). 값은 단말 정수. */
function Slider({ c, it }: { c: FieldCtx; it: SettingsItem }) {
  const v = parseText(it, shownText(c, it));
  return (
    <input type="range" min={it.min} max={it.max} step={sliderStep(it)} value={v ?? it.min} disabled={c.disabled}
      aria-label={`${it.label} 선택바`} onChange={(e) => c.set(it.key, toText(it, Number(e.target.value)))} />
  );
}

function FieldNote({ c, keys, help }: { c: FieldCtx; keys: string[]; help?: string }) {
  const own = keys.map((k) => c.serverErr[k]).filter(Boolean);
  const range = c.values
    ? keys.map((k) => {
        const it = allItems(c.schema).find((i) => i.key === k);
        const e = it ? rangeError(it, parseText(it, c.edit[k])) : null;
        return e && it ? `${it.label}: ${e}` : null;
      }).filter(Boolean)
    : [];
  return (
    <>
      {help && <div className="help">{help}</div>}
      {[...range, ...own].map((m, i) => <div key={i} className="help c-alarm">{m}</div>)}
    </>
  );
}

/** 한 줄 = 항목 이름 | 선택바 | 숫자 | 단위. */
function Field({ c, it }: { c: FieldCtx; it: SettingsItem }) {
  const dirty = isDirty(c, it);
  const bad = c.bad.has(it.key) || !!c.serverErr[it.key] || (!!c.values && !!rangeError(it, parseText(it, c.edit[it.key])));
  return (
    <div className={`fld ${bad ? "bad" : ""} ${c.values ? "" : "ref"}`.trim()} title={`${it.key} · 범위 ${toText(it, it.min)}~${toText(it, it.max)} · 기본 ${toText(it, it.default)}`}>
      <label>{dirty && <span className="mk edit" title="편집함, 아직 안 보냄" />}{it.label}</label>
      <div className="in"><Slider c={c} it={it} /></div>
      <NumInput c={c} it={it} />
      <span className="unit">{it.unit}</span>
      <FieldNote c={c} keys={[it.key]} help={it.help || undefined} />
    </div>
  );
}

const pad2 = (n: number | null) => (n === null ? "" : String(n).padStart(2, "0"));

/** 다단계 한 줄 = "1단계" | [시각(시계 아이콘) + 선택바] | 숫자 | %. 시·분은 시각 칸 하나로 고른다. */
function TimeField({ c, h, m, pwm }: { c: FieldCtx; h: SettingsItem; m: SettingsItem; pwm?: SettingsItem }) {
  const its = [h, m, ...(pwm ? [pwm] : [])];
  const dirty = its.some((it) => isDirty(c, it));
  const bad = its.some((it) => c.bad.has(it.key) || c.serverErr[it.key]);
  const label = h.label.replace(/\s*시$/, "");
  const hv = parseText(h, shownText(c, h));
  const mv = parseText(m, shownText(c, m));
  const time = hv === null || mv === null ? "" : `${pad2(hv)}:${pad2(mv)}`;
  return (
    <div className={`fld tfld ${bad ? "bad" : ""} ${c.values ? "" : "ref"}`.trim()} title={its.map((i) => i.key).join(" · ")}>
      <label>{dirty && <span className="mk edit" title="편집함, 아직 안 보냄" />}{label}</label>
      <div className="in">
        <input type="time" className="tm" value={time} disabled={c.disabled} aria-label={`${label} 시각`} step={60}
          onChange={(e) => {
            const mt = /^(\d{1,2}):(\d{2})/.exec(e.target.value);
            if (!mt) return;
            c.set(h.key, String(Number(mt[1])));
            c.set(m.key, String(Number(mt[2])));
          }} />
        {pwm && <Slider c={c} it={pwm} />}
      </div>
      {pwm ? <NumInput c={c} it={pwm} /> : <span />}
      <span className="unit">{pwm ? pwm.unit : ""}</span>
      <FieldNote c={c} keys={its.map((i) => i.key)} />
    </div>
  );
}

/** 오늘 밤 주등 밝기 막대(문제점 #2 그림) — 점등 → 시작 밝기 → 1~4단계 → 소등.
 *  점등·소등 시각은 단말 표 조건으로 서버가 계산한 오늘 행 + 시작/종료 Offset. 표 조건을 모르면 그리지 않는다. */
export function StageBar({ schema, pv, tbl }: { schema: SettingsSchema; pv: (k: string) => number | null; tbl: SettingsTbl | null }) {
  const [today, setToday] = useState<{ on: string; off: string } | null>(null);
  const sig = tbl ? `${tbl.lat_e6}|${tbl.lon_e6}|${tbl.on}|${tbl.off}` : "";
  useEffect(() => {
    if (!tbl) return;
    let alive = true;
    api.schedulePreview({ lat: tbl.lat_e6 / 1e6, lon: tbl.lon_e6 / 1e6, on: tbl.on, off: tbl.off })
      .then((p) => {
        // 미리보기는 달마다 두 날(1일·16일)만 준다 — 오늘과 가장 가까운 날(점등 시각 차이 수 분)을 쓴다.
        const doy = (m: number, d: number) => Math.round((Date.UTC(2026, m - 1, d) - Date.UTC(2026, 0, 1)) / 864e5);
        const now = new Date();
        const t = doy(now.getMonth() + 1, now.getDate());
        const dist = (x: { month: number; day: number }) => {
          const a = Math.abs(doy(x.month, x.day) - t);
          return Math.min(a, 365 - a);
        };
        const r = [...p.rows].sort((a, b) => dist(a) - dist(b))[0];
        if (alive && r) setToday({ on: r.on, off: r.off });
      })
      .catch(() => alive && setToday(null));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);
  if (!tbl || !today) return <div className="cap">오늘 밤 막대는 단말에서 읽은 뒤(표 조건을 알아야) 보인다.</div>;

  const toMin = (t: string) => {
    const [a, b] = t.split(":").map(Number);
    return a * 60 + b;
  };
  const items = allItems(schema);
  const startOfs = items.find((i) => i.unit === "분" && /start/.test(i.key));
  const stopOfs = items.find((i) => i.unit === "분" && /stop/.test(i.key));
  // 시작 밝기 = 실제 출력 계산식의 곱하는 항목(schema.calc.keys 마지막)
  const startPwm = items.find((i) => i.key === schema.calc.keys[schema.calc.keys.length - 1]);
  const on = toMin(today.on) + (startOfs ? pv(startOfs.key) ?? 0 : 0);
  const off = toMin(today.off) + (stopOfs ? pv(stopOfs.key) ?? 0 : 0);
  const night = ((off - on) % 1440 + 1440) % 1440 || 1440;
  const rel = (t: number) => ((t - on) % 1440 + 1440) % 1440; // 점등부터 몇 분 뒤
  const segs: { from: number; pct: number | null }[] = [{ from: 0, pct: startPwm ? pv(startPwm.key) : null }];
  stageKeys(schema).forEach((st) => {
    const hv = pv(st.h), mv = pv(st.m);
    const p = items.find((i) => i.key === st.h.replace(/_h$/, "_pwm"));
    if (hv === null || mv === null) return;
    const r = rel(hv * 60 + mv);
    if (r > 0 && r < night) segs.push({ from: r, pct: p ? pv(p.key) : null });
  });
  segs.sort((a, b) => a.from - b.from);
  const hhmm = (t: number) => `${pad2(Math.floor((((t % 1440) + 1440) % 1440) / 60))}:${pad2((((t % 1440) + 1440) % 1440) % 60)}`;
  return (
    <div className="stbar" title="DIP4 가 ON 일 때 다단계가 쓰인다">
      <div className="bar">
        {segs.map((sg, i) => {
          const to = i + 1 < segs.length ? segs[i + 1].from : night;
          const w = ((to - sg.from) / night) * 100;
          const a = sg.pct === null ? 0.15 : 0.2 + (sg.pct / 100) * 0.8;
          return <i key={i} style={{ width: `${w}%`, background: `rgba(245, 180, 0, ${a})` }} title={`${hhmm(on + sg.from)}~${hhmm(on + to)} ${sg.pct ?? "-"}%`}>{w > 7 ? `${sg.pct ?? "-"}%` : ""}</i>;
        })}
      </div>
      <div className="lg"><span>점등 {hhmm(on)}</span><span>오늘 밤 주등</span><span>소등 {hhmm(off)}</span></div>
    </div>
  );
}

/** 그룹 항목을 widget 에 따라 줄로. time_h 다음 time_m (그 다음 같은 번호 slider) 는 한 줄. */
export function GroupFields({ c, items }: { c: FieldCtx; items: SettingsItem[] }) {
  const rows: ReactNode[] = [];
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const nx = items[i + 1];
    if (it.widget === "time_h" && nx?.widget === "time_m") {
      const base = it.key.replace(/_h$/, "");
      const p = items[i + 2];
      const pwm = p && p.key.startsWith(base) && p.widget === "slider" ? p : undefined;
      rows.push(<TimeField key={it.key} c={c} h={it} m={nx} pwm={pwm} />);
      i += pwm ? 2 : 1;
    } else rows.push(<Field key={it.key} c={c} it={it} />);
  }
  return <>{rows}</>;
}

// ---------------- 오른쪽: 선택 단말 설정 ----------------
interface SentNote {
  kind: "SETTINGS_GET" | "SETTINGS_SET";
  label: string;
  r: SettingsSent;
}

const byText = (by: string) => (by === "device_read" ? "단말에서 읽음" : by === "server_write" ? "서버가 씀" : by);

function SettingsPanel({ uuid, schema, onSelect }: { uuid: string; schema: SettingsSchema; onSelect: (u: string) => void }) {
  const [dev, setDev] = useState<Device | null>(null);
  const [st, setSt] = useState<DeviceSettings | null>(null);
  const [hist, setHist] = useState<SettingsHistoryRow[] | null>(null);
  const [ev, setEv] = useState<DeviceEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [edit, setEdit] = useState<Record<string, string>>({});
  const loadedJson = useRef<string>("");
  const dirtyCount = useRef(0);
  const [sent, setSent] = useState<SentNote | null>(null);
  const [actErr, setActErr] = useState<string | null>(null);
  const [actMsg, setActMsg] = useState<string | null>(null);
  const [serverErr, setServerErr] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  // 1년 스케줄 조건
  const [withTbl, setWithTbl] = useState(false);
  const [tbl, setTbl] = useState({ region: "", lat: "", lon: "", on: "", off: "" });
  const tblTouched = useRef(false);
  const [prev, setPrev] = useState<SchedulePreview | null>(null);
  const [prevErr, setPrevErr] = useState<string | null>(null);

  const items = useMemo(() => allItems(schema), [schema]);
  const itemOf = useCallback((k: string) => items.find((i) => i.key === k), [items]);

  const load = useCallback(async () => {
    try {
      const [d, s] = await Promise.all([api.getDevice(uuid), api.getSettings(uuid)]);
      setDev(d);
      setSt(s);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [uuid]);

  // 이력·통신 기록은 설정 상태가 바뀔 때만 다시 읽는다
  const histKey = st ? `${st.sync}|${st.read_at}|${st.last_result_at}|${st.pending?.seq ?? ""}` : "";
  useEffect(() => {
    if (!histKey) return;
    api.settingsHistory(uuid, 100).then(setHist).catch(() => setHist([]));
    api.events(uuid, 100).then((e) => setEv(e.filter((x) => x.kind.includes("SETTINGS")))).catch(() => setEv([]));
  }, [uuid, histKey]);

  // 대기 중(pending 또는 writing)이면 3초, 아니면 10초
  const fast = !!st && (!!st.pending || st.sync === "writing");
  useEffect(() => {
    load();
    const id = setInterval(load, fast ? POLL_MS : REFRESH_MS);
    return () => clearInterval(id);
  }, [load, fast]);

  const values = st?.values ?? null;

  // 파싱한 편집값(values 가 없으면 전부 null)
  const parsed = useMemo(() => {
    const o: Record<string, number | null> = {};
    items.forEach((it) => (o[it.key] = values ? parseText(it, edit[it.key]) : null));
    return o;
  }, [items, edit, values]);
  const dirtyKeys = values ? items.filter((it) => parsed[it.key] !== values[it.key]).map((i) => i.key) : [];
  const dirty = dirtyKeys.length > 0;
  dirtyCount.current = dirtyKeys.length;

  // 서버 값이 바뀌었고 편집 중이 아니면 입력칸을 서버 값으로(편집 중이면 편집을 지킨다)
  useEffect(() => {
    const j = values ? JSON.stringify(values) : "";
    if (j === loadedJson.current) return;
    const keepEdits = loadedJson.current !== "" && !!values && dirtyCount.current > 0;
    if (!keepEdits) {
      const e: Record<string, string> = {};
      if (values) items.forEach((it) => (e[it.key] = toText(it, values[it.key])));
      setEdit(e);
    }
    loadedJson.current = j;
  }, [values, items]);

  // 표 조건: 단말 tbl 로 채운다(없으면 비워 둔다 — 지어내지 않는다)
  const tblSig = st?.tbl ? `${st.tbl.region}|${st.tbl.lat_e6}|${st.tbl.lon_e6}|${st.tbl.on}|${st.tbl.off}|${st.tbl.crc}` : "";
  useEffect(() => {
    if (tblTouched.current) return;
    const t = st?.tbl;
    setTbl(t
      ? { region: t.region, lat: String(t.lat_e6 / 1e6), lon: String(t.lon_e6 / 1e6), on: String(t.on), off: String(t.off) }
      : { region: "", lat: "", lon: "", on: "", off: "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tblSig]);

  const rules: RuleError[] = values ? checkRules(schema, parsed) : [];
  const badKeys = new Set(rules.flatMap((r) => r.keys));
  const rangeBad = values ? items.filter((it) => rangeError(it, parsed[it.key])) : [];

  // 표 조건 검사
  const yt = schema.year_table.inputs;
  const tblIn = (k: string) => yt.find((i) => i.key === k);
  const regionMax = tblIn("region")?.max_bytes_utf8 ?? 47;
  const tblErrs: Record<string, string> = {};
  {
    const re = regionError(tbl.region, regionMax);
    if (re) tblErrs.region = re;
    const chk = (k: "lat" | "lon" | "on" | "off", ik: string, int: boolean) => {
      const d = tblIn(ik);
      const v = tbl[k].trim() === "" ? NaN : Number(tbl[k]);
      const lo = d?.min ?? -180, hi = d?.max ?? 180;
      if (!Number.isFinite(v)) tblErrs[k] = "숫자를 넣는다";
      else if (int && !Number.isInteger(v)) tblErrs[k] = "정수 분";
      else if (v < lo || v > hi) tblErrs[k] = `${lo} ~ ${hi}`;
    };
    chk("lat", "lat", false);
    chk("lon", "lon", false);
    chk("on", "on_corr", true);
    chk("off", "off_corr", true);
  }
  const tblOk = Object.keys(tblErrs).length === 0;
  const tblBody = (): SettingsTblIn => ({ region: tbl.region, lat: Number(tbl.lat), lon: Number(tbl.lon), on: Number(tbl.on), off: Number(tbl.off) });

  const setTblField = (k: keyof typeof tbl, v: string) => {
    tblTouched.current = true;
    setPrev(null);
    // "37.5665, 126.9780" 을 위도 칸에 붙이면 둘로 나눈다(명세 §3.1)
    if (k === "lat") {
      const mt = /^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$/.exec(v);
      if (mt) {
        setTbl((t) => ({ ...t, lat: mt[1], lon: mt[2] }));
        return;
      }
    }
    setTbl((t) => ({ ...t, [k]: v }));
  };
  const resetTbl = () => {
    tblTouched.current = false;
    const t = st?.tbl;
    setPrev(null);
    setTbl(t
      ? { region: t.region, lat: String(t.lat_e6 / 1e6), lon: String(t.lon_e6 / 1e6), on: String(t.on), off: String(t.off) }
      : { region: "", lat: "", lon: "", on: "", off: "" });
  };

  // ---- 상태 판단 ----
  const state = dev?.state;
  const canRead = state === "PENDING" || state === "ACTIVE";
  const pending = st?.pending ?? null;
  const writeBlock: string | null =
    !st ? "불러오는 중" :
    state !== "ACTIVE" ? "쓰기는 운영(ACTIVE) 단말만" :
    !values ? "아직 단말에서 읽지 않았다 — 먼저 '단말에서 읽기'" :
    pending ? `${pending.kind} 명령 번호 ${pending.seq} 응답 대기 중` :
    rangeBad.length ? `범위를 벗어난 값: ${rangeBad.map((i) => i.label).join(", ")}` :
    rules.length ? "규칙 위반 — 빨간 줄을 고친다" :
    withTbl && !tblOk ? "1년 스케줄 조건을 고친다" :
    !dirty && !withTbl ? "바뀐 값이 없다" : null;

  function handleErr(e: unknown) {
    setActErr(errorText(e));
    if (e instanceof ApiErrorException) {
      // detail.key = 항목 키, 표 조건은 "tbl.lat" 꼴(detail.field 로 올 때도 있다) → 표 칸 이름으로
      const d = (e.err.detail ?? {}) as { key?: string; field?: string; rule?: string };
      const k = (d.key ?? d.field)?.replace(/^tbl\./, "");
      if (k) setServerErr({ [k]: `서버 ${e.err.code}: ${e.err.message}` });
      else if (d.rule) {
        const rule = schema.rules.find((r) => r.id === d.rule);
        setServerErr({ __rule: `서버 규칙 위반 ${d.rule}${rule ? ` (${rule.text})` : ""} — ${e.err.message}` });
      }
    }
  }

  async function run(fn: () => Promise<void>) {
    setActErr(null);
    setActMsg(null);
    setServerErr({});
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      handleErr(e);
    } finally {
      setBusy(false);
    }
  }

  const doRead = () =>
    run(async () => {
      const r = await api.readSettings(uuid);
      setSent({ kind: "SETTINGS_GET", label: "단말에서 읽기", r });
      await load();
    });

  function doWrite() {
    if (writeBlock || !values) return;
    const vals: Record<string, number> = {};
    items.forEach((it) => (vals[it.key] = parsed[it.key]!));
    const lines = dirtyKeys.map((k) => `${itemOf(k)?.label ?? k}: ${fmtVal(itemOf(k), values[k])} → ${fmtVal(itemOf(k), vals[k])}`);
    if (withTbl) lines.push(`1년 스케줄 조건: ${tbl.region} · ${tbl.lat}, ${tbl.lon} · 보정 ${tbl.on}/${tbl.off}분`);
    if (!confirm(`${dev?.site ?? uuid}\n단말에 씁니다(25개 전부${withTbl ? " + 표 조건" : ""}). 단말은 받자마자 적용하고 Flash 에 저장합니다.\n\n${lines.join("\n")}`)) return;
    run(async () => {
      const r = await api.putSettings(uuid, { values: vals, tbl: withTbl ? tblBody() : null });
      setSent({ kind: "SETTINGS_SET", label: "단말에 쓰기", r });
      if (withTbl) {
        setWithTbl(false);
        tblTouched.current = false;
      }
      await load();
    });
  }

  function doAccept() {
    if (!confirm("단말이 보고한 값을 DB 에 받아들입니다(서버 값이 단말 값으로 바뀜). 계속할까요?")) return;
    run(async () => {
      await api.acceptSettings(uuid);
      dirtyCount.current = 0;
      loadedJson.current = "";
      setActMsg("단말 값을 받아들였다 — DB = 단말 값");
      await load();
    });
  }

  function doRevert() {
    if (!confirm("서버(DB) 값을 단말에 다시 씁니다(SETTINGS_SET). 현장에서 바꾼 값은 사라집니다. 계속할까요?")) return;
    run(async () => {
      const r = await api.revertSettings(uuid);
      setSent({ kind: "SETTINGS_SET", label: "서버 값으로 되돌리기", r });
      await load();
    });
  }

  function discardEdits() {
    if (!values) return;
    const e: Record<string, string> = {};
    items.forEach((it) => (e[it.key] = toText(it, values[it.key])));
    setEdit(e);
    setServerErr({});
  }

  async function doPreview() {
    setPrevErr(null);
    setPrev(null);
    if (tblErrs.lat || tblErrs.lon || tblErrs.on || tblErrs.off) {
      setPrevErr("위도·경도·보정을 먼저 고친다(보정은 비우지 말고 0)");
      return;
    }
    try {
      setPrev(await api.schedulePreview({ lat: Number(tbl.lat), lon: Number(tbl.lon), on: Number(tbl.on), off: Number(tbl.off) }));
    } catch (e) {
      setPrevErr(errorText(e));
    }
  }

  const ctx: FieldCtx = {
    schema, edit, values, bad: badKeys, serverErr,
    disabled: !values || busy,
    set: (k, t) => {
      setEdit((e) => ({ ...e, [k]: t }));
      setServerErr((s) => (s[k] || s.__rule ? {} : s));
    },
  };

  if (error && !st) return <Card title="운전 설정" className="full"><div className="err">{error}</div></Card>;
  if (!st || !dev) return <Card title="운전 설정" className="full"><div className="muted">불러오는 중…</div></Card>;

  const sync = st.sync;
  const t = st.tbl;
  const calcKeys = schema.calc.keys;
  const mult = calcKeys[calcKeys.length - 1];
  const bases = calcKeys.slice(0, -1);
  const pv = (k: string): number | null => (values ? parsed[k] : itemOf(k)?.default ?? null);
  const dip = st.dev?.dip ?? null;
  const bat = st.dev?.bat ?? null;
  const stages = stageKeys(schema);
  const stageGroupId = schema.groups.find((g) => g.items.some((i) => stages.some((s) => s.h === i.key)))?.id;
  const calcGroupId = schema.groups.find((g) => g.items.some((i) => i.key === bases[0]))?.id;
  const battGroupId = schema.groups.find((g) => g.items.some((i) => i.unit === "V"))?.id;
  const ssDiff = st.ss_known !== null && st.ss_telemetry !== null && st.ss_known !== st.ss_telemetry;
  const sentDone = !!sent && (!pending || pending.seq !== sent.r.seq) && sync !== "writing";
  const tiMin = Math.max(1, Math.round(dev.ti_effective / 60));

  const groupMeta = (id: string, n: number): ReactNode => {
    if (id === stageGroupId && dip !== null)
      return <span className={dip & 0x08 ? "c-ok" : "c-warn"}>DIP4 {dip & 0x08 ? "ON — 다단계 사용" : "OFF — 다단계 안 씀"}</span>;
    if (id === battGroupId && bat) return <span>배터리 계통 <b>{bat}V</b> — {bat}V 값이 쓰인다</span>;
    return `${n}개`;
  };

  return (
    <>
      {/* ---------- 상태 줄 ---------- */}
      <Card
        className="full"
        title={<span className="bar2" style={{ gap: 12 }}>{dev.site ?? "(시설명 없음)"} <SyncBadge sync={sync} /></span>}
        meta={<>
          <span className="mono">{uuid}</span><StateBadge state={dev.state} /><OnlineMark on={dev.is_online} />
          <button type="button" className="btn sm" onClick={() => onSelect(uuid)}>단말 상세</button>
        </>}
      >
        <div className="grid5">
          <Met l="마지막 전체 읽기" v={st.read_at ? localTime(st.read_at) : "읽지 않음"} h={st.read_at ? relTime(st.read_at) : "서버는 아직 이 단말 값을 모른다"} cls={st.read_at ? "" : "o"} />
          <Met l="설정 지문 sh (DB / 단말)" v={<span className="mono">{str(st.sh_db)} / {str(st.sh_device)}</span>}
            h={st.sh_db && st.sh_device ? (st.sh_db === st.sh_device ? "단말과 같음" : "단말과 다름") : "-"} cls={st.sh_db && st.sh_device && st.sh_db !== st.sh_device ? "a" : ""} />
          <Met l="저장 번호 ss (기준 / Telemetry)" v={`${str(st.ss_known)} / ${str(st.ss_telemetry)}`}
            h={ssDiff ? "단말과 다름 — 현장에서 저장함" : "현장 저장 신호"} cls={ssDiff ? "w" : ""} />
          <Met l="대기 중 요청" v={pending ? `${pending.kind} #${pending.seq}` : "없음"}
            h={pending ? `시도 ${pending.attempts}/3 · 보냄 ${localTime(pending.sent_at)}` : "무응답 30초면 새 명령 번호로 재발송(최대 3회)"} cls={pending ? "w" : ""} />
          <Met l="마지막 결과" v={st.last_result ?? "-"} h={localTime(st.last_result_at)} cls={st.last_result && st.last_result !== "OK" ? "a" : st.last_result === "OK" ? "k" : ""} />
        </div>

        <div className="bar2">
          <button type="button" className="btn" disabled={!canRead || !!pending || busy} onClick={doRead}
            title={canRead ? "SETTINGS_GET — 25개 값·표 조건·현장 스위치를 한 번에" : "읽기는 승인 대기(PENDING)·운영(ACTIVE) 단말만"}>
            단말에서 읽기
          </button>
          <button type="button" className="btn pri" disabled={!!writeBlock || busy} onClick={doWrite} title={writeBlock ?? "SETTINGS_SET — 25개 전부(+ 표 조건을 고르면 tbl)"}>
            단말에 쓰기{dirty ? ` (${dirtyKeys.length}개 바뀜${withTbl ? " + 표" : ""})` : withTbl ? " (표 조건)" : ""}
          </button>
          {dirty && <button type="button" className="btn" onClick={discardEdits}>편집 취소</button>}
          <span className="sp" />
          <span className="cap">{dirty && <><span className="mk edit" /> 편집, 안 보냄 · </>}쓰기 = 적용 + Flash 저장 한 번에(LTE)</span>
        </div>
        {writeBlock && (dirty || withTbl) && <div className="cap c-warn">쓰기 막힘: {writeBlock}</div>}
        {!canRead && <div className="cap c-warn">이 단말은 {dev.state} — 읽기는 PENDING·ACTIVE, 쓰기는 ACTIVE 만.</div>}
        {canRead && state === "PENDING" && values && <div className="cap">승인 대기 단말 — 읽기만 된다. 승인(ACTIVE) 뒤에 쓸 수 있다.</div>}

        {sync === "unknown" && (
          <div className="confirm">
            <b>읽지 않음</b> — 서버는 이 단말의 운전 설정을 아직 모른다. 단말은 출하 기본값이나 현장 PC 도구로 넣은 값으로 이미 운전 중이다.
            기본값을 가정해 쓰면 현장 설정을 덮어쓰므로 <b>먼저 단말에서 읽는다.</b> 아래 회색 값은 펌웨어 기본값(참고)일 뿐 단말 값이 아니다.
          </div>
        )}
        {sync === "local_saved" && (
          <div className="confirm">
            <b>현장에서 저장함</b> — Telemetry 의 스케줄 저장 번호(ss {str(st.ss_telemetry)})가 서버 기준(ss {str(st.ss_known)})과 다르다.
            현장에서 PC 도구·OLED·엔코더로 저장했다. 값이 바뀌었는지는 <b>다시 읽어야</b> 안다(자동으로 읽지 않는다).
            <div className="row"><button type="button" className="btn pri" disabled={!canRead || !!pending || busy} onClick={doRead}>다시 읽기</button></div>
          </div>
        )}
        {sync === "device_changed" && (
          <div className="confirm">
            <b>단말과 다름</b> — 읽어 보니 단말 값이 DB 와 다르다(sh {str(st.sh_device)} ≠ {str(st.sh_db)}). 서버는 자동으로 덮어쓰지 않는다. 어느 쪽을 기준으로 할지 고른다.
            <div style={{ overflowX: "auto", marginTop: 8 }}>
              <table className="mini">
                <thead><tr><th>항목</th><th className="n">서버(DB)</th><th className="n">단말</th></tr></thead>
                <tbody>
                  {st.diff.map((d) => (
                    <tr key={d.key}><td>{itemOf(d.key)?.label ?? d.key} <small className="muted">{d.key}</small></td>
                      <td className="n">{fmtVal(itemOf(d.key), d.db)}</td><td className="n c-alarm">{fmtVal(itemOf(d.key), d.device)}</td></tr>
                  ))}
                  {st.diff.length === 0 && <tr><td colSpan={3} className="muted">값 차이 목록 없음</td></tr>}
                </tbody>
              </table>
            </div>
            <div className="row">
              <button type="button" className="btn" disabled={busy} onClick={doAccept} title="POST …/settings/accept — DB ← 단말 보고값">단말 값 받아들이기</button>
              <button type="button" className="btn pri" disabled={busy || state !== "ACTIVE" || !!pending} onClick={doRevert} title="POST …/settings/revert — DB 값을 SETTINGS_SET">서버 값으로 되돌리기</button>
            </div>
          </div>
        )}

        {sent && (
          <div className="recv">
            <div>발행함: <b>{sent.label} · {sent.kind} 명령 번호 {sent.r.seq}</b> · {localTime(sent.r.sent_at)}
              {sent.r.sh_expected ? <> · 예상 sh <span className="mono">{sent.r.sh_expected}</span></> : null}
              {sent.r.payload_bytes ? ` · ${sent.r.payload_bytes}B` : ""}</div>
            <div>단말 응답: {sentDone
              ? <b className={st.last_result === "OK" ? "c-ok" : "c-alarm"}>{st.last_result ?? "-"} · {syncLabel(sync)} ({localTime(st.last_result_at)})</b>
              : <b className="c-warn">대기 중 — 단말은 다음 송신 뒤 받을 수 있음(최대 ti 약 {tiMin}분). 3초마다 확인{pending ? ` · 시도 ${pending.attempts}/3` : ""}</b>}
            </div>
          </div>
        )}
        {actMsg && <div className="okl">{actMsg}</div>}
        {actErr && <div className="err">{actErr}</div>}
        {serverErr.__rule && <div className="err">{serverErr.__rule}</div>}
        {rules.map((r) => <div key={r.id} className="err">규칙 {r.id}: {r.text}</div>)}
      </Card>

      {/* ---------- 설정 그룹(스키마 순서) ---------- */}
      <div className="grp2 full">
        {schema.groups.map((g) => (
          <Card key={g.id} title={g.title} meta={groupMeta(g.id, g.items.length)}>
            <GroupFields c={ctx} items={g.items} />
            {g.id === stageGroupId && <StageBar schema={schema} pv={pv} tbl={t} />}
            {g.id === calcGroupId && mult && (
              <div className={`out ${values ? "" : "ref"}`.trim()} style={{ marginTop: "auto" }} title={schema.calc.text}>
                <span>실제 출력 (x {pv(mult) ?? "-"}%)</span>
                {bases.map((k) => {
                  const b = pv(k), m = pv(mult);
                  return <span key={k}>{itemOf(k)?.label ?? k} <b>{b !== null && m !== null ? `${effPwm(b, m)}%` : "-"}</b></span>;
                })}
              </div>
            )}
          </Card>
        ))}
      </div>

      {/* ---------- 1년 스케줄 ---------- */}
      <Card className="full" title="1년 스케줄"
        meta={t ? <span>근거: {t.region || "-"} · {(t.lat_e6 / 1e6).toFixed(4)}, {(t.lon_e6 / 1e6).toFixed(4)} · 보정 {signed(t.on)} / {signed(t.off)}분 · {TBL_SRC[t.src] ?? `src ${t.src}`}</span> : "단말 표 정보 없음(읽지 않음)"}>
        <div className="cap">표(372일)는 주고받지 않는다. 조건(지역·좌표·보정)만 보내면 단말이 같은 식으로 직접 계산하고 CRC 가 같을 때만 저장한다.</div>
        {t ? (
          <div className="grid4">
            <Met l="지역" v={t.region || "-"} h={`${utf8Bytes(t.region)}/${regionMax}B`} />
            <Met l="표 계산 좌표" v={`${(t.lat_e6 / 1e6).toFixed(6)}, ${(t.lon_e6 / 1e6).toFixed(6)}`} h="설치 좌표(CONFIG lat/lon)와 다를 수 있다" />
            <Met l="보정 (점등 / 소등)" v={`${signed(t.on)} / ${signed(t.off)}분`} h={`만든 쪽: ${TBL_SRC[t.src] ?? t.src} (src ${t.src}) · ss ${t.ss}`} />
            <Met l="표 CRC (단말 / 서버 계산)" v={<span className="mono">{t.crc} / {str(t.crc_expected)}</span>}
              h={t.matches === null ? "비교 불가" : t.matches ? "✓ 조건대로 만든 표" : "✗ 현장에서 손댄 표"} cls={t.matches === false ? "a" : t.matches ? "k" : ""} />
          </div>
        ) : <div className="muted">아직 단말에서 읽지 않아 표 조건을 모른다.</div>}

        <div className="form5">
          <label><span>지역 · <b className={utf8Bytes(tbl.region) > regionMax ? "c-alarm" : ""}>{utf8Bytes(tbl.region)}/{regionMax}B</b></span>
            <CitySelect value={tbl.region} disabled={!values} onPick={(c) => {
              // 시·군을 고르면 지역명 + 대표 좌표(시청·군청). 좌표는 아래에서 고칠 수 있다(문제점 18번).
              tblTouched.current = true;
              setPrev(null);
              setTbl((t) => ({ ...t, region: c.label, lat: String(c.lat), lon: String(c.lon) }));
            }} />
            {tblErrs.region && (withTbl || tbl.region) && <small className="c-alarm">{tblErrs.region}</small>}
            {serverErr.region && <small className="c-alarm">{serverErr.region}</small>}</label>
          <label><span>위도</span>
            <input className="inp" value={tbl.lat} placeholder="37.5665 (또는 '37.5665, 126.978')" disabled={!values} onChange={(e) => setTblField("lat", e.target.value)} />
            {tblErrs.lat && tbl.lat && <small className="c-alarm">{tblErrs.lat}</small>}
            {serverErr.lat && <small className="c-alarm">{serverErr.lat}</small>}</label>
          <label><span>경도</span>
            <input className="inp" value={tbl.lon} placeholder="126.978" disabled={!values} onChange={(e) => setTblField("lon", e.target.value)} />
            {tblErrs.lon && tbl.lon && <small className="c-alarm">{tblErrs.lon}</small>}
            {serverErr.lon && <small className="c-alarm">{serverErr.lon}</small>}</label>
          <label><span>점등 보정 (표, 분)</span>
            <input className="inp" type="number" min={-180} max={180} value={tbl.on} placeholder="0" disabled={!values} onChange={(e) => setTblField("on", e.target.value)} />
            {tblErrs.on && tbl.on && <small className="c-alarm">{tblErrs.on}</small>}
            {serverErr.on && <small className="c-alarm">{serverErr.on}</small>}</label>
          <label><span>소등 보정 (표, 분)</span>
            <input className="inp" type="number" min={-180} max={180} value={tbl.off} placeholder="0" disabled={!values} onChange={(e) => setTblField("off", e.target.value)} />
            {tblErrs.off && tbl.off && <small className="c-alarm">{tblErrs.off}</small>}
            {serverErr.off && <small className="c-alarm">{serverErr.off}</small>}</label>
        </div>
        <div className="bar2">
          <button type="button" className="btn" onClick={doPreview} disabled={!tbl.lat || !tbl.lon}>미리보기</button>
          <label className="chk2" title="체크하면 SETTINGS_SET 에 tbl(조건 + 서버 계산 CRC)을 싣는다. 안 하면 표는 그대로 둔다">
            <input type="checkbox" checked={withTbl} disabled={!values} onChange={(e) => setWithTbl(e.target.checked)} />표 조건도 함께 쓰기
          </label>
          {tblTouched.current && <button type="button" className="btn sm" onClick={resetTbl}>단말 값으로</button>}
          {withTbl && !tblOk && <span className="cap c-alarm">조건을 고쳐야 쓸 수 있다</span>}
          <span className="sp" />
          <span className="cap">보정은 표에 들어가고, 시작/종료 Offset 은 운전 중에 더한다(별개). 연도 입력 없음.</span>
        </div>
        {prevErr && <div className="err">{prevErr}</div>}
        {prev && (
          <>
            <div className="cap">
              미리보기 표 CRC <span className="mono">{prev.crc}</span> (lat_e6 {prev.lat_e6}, lon_e6 {prev.lon_e6})
              {t && (prev.crc === t.crc ? <b className="c-ok"> — 지금 단말 표와 같다</b> : <b className="c-warn"> — 지금 단말 표({t.crc})와 다르다. 쓰면 표가 바뀐다</b>)}
            </div>
            <div style={{ maxHeight: 360, overflow: "auto" }}>
              <table className="mini">
                <thead><tr><th>날짜</th><th>점등</th><th>소등</th><th className="n">점등 시간</th></tr></thead>
                <tbody>
                  {prev.rows.map((r) => (
                    <tr key={`${r.month}-${r.day}`}><td>{r.month}월 {r.day}일</td><td>{r.on}</td><td>{r.off}</td><td className="n">{hoursText(r.hours)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>

      {/* ---------- 현장 스위치(읽기 전용) ---------- */}
      <Card title="현장 스위치 (읽기 전용)" meta="SETTINGS dev · 서버가 바꿀 수 없다">
        {st.dev ? (
          <>
            <div className="dips">
              {Array.from({ length: 8 }, (_, i) => {
                const on = dip !== null && ((dip >> i) & 1) === 1;
                return <span key={i} className={on ? "on" : ""} title={`DIP${i + 1} ${on ? "ON" : "OFF"}${i === 3 ? " — 다단계 밝기" : ""}`}><b>DIP{i + 1}</b>{on ? "ON" : "OFF"}</span>;
              })}
            </div>
            <div className="cap">dip = {dip} (DIP1 = bit0 … DIP8 = bit7, ON = 1). DIP4 = 다단계 밝기 사용.</div>
            <Met l="배터리 계통" v={bat ? `${bat}V` : "아직 모름"} h={bat ? `${bat}V 차단/복귀 전압이 쓰인다` : "MPPT 에서 아직 못 받음(0)"} />
          </>
        ) : <div className="muted">읽지 않음</div>}
      </Card>

      {/* ---------- 변경 이력 ---------- */}
      <Card title="변경 이력" meta={`${hist ? hist.length : "-"}건 · 현장 변경도 다시 읽을 때 남는다`}>
        <div style={{ maxHeight: 320, overflow: "auto" }}>
          <table className="mini">
            <thead><tr><th>시각</th><th>누가</th><th>항목</th><th className="n">이전 → 새 값</th><th>비고</th></tr></thead>
            <tbody>
              {(hist ?? []).map((h, i) => (
                <tr key={i}>
                  <td title={h.changed_at}>{localTime(h.changed_at)}</td><td>{byText(h.by)}</td>
                  <td title={h.key}>{itemOf(h.key)?.label ?? h.key}</td>
                  <td className="n">{fmtVal(itemOf(h.key), h.old)} → <b>{fmtVal(itemOf(h.key), h.new)}</b></td>
                  <td className="wrap">{str(h.note)}</td>
                </tr>
              ))}
              {hist && hist.length === 0 && <tr><td colSpan={5} className="muted">이력 없음</td></tr>}
              {!hist && <tr><td colSpan={5} className="muted">불러오는 중…</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>

      {/* ---------- 통신 기록 ---------- */}
      <Card className="full" title="통신 기록" meta="이 단말의 SETTINGS 이벤트 · cmd / result">
        <ul className="log">
          {ev.map((e) => (
            <li key={e.id} title={JSON.stringify(e.payload)}>
              <span className="t">{localTime(e.received_at)}</span>
              <span className={e.kind.endsWith("_GET") || e.kind.endsWith("_SET") ? "dn" : "up"}>{e.kind}</span>
              <span className="body">{eventSummary(e.kind, e.payload)}</span>
            </li>
          ))}
          {ev.length === 0 && <li><span className="t">-</span><span /><span className="muted">SETTINGS 이벤트 없음</span></li>}
        </ul>
      </Card>
    </>
  );
}

// ---------------- 페이지 ----------------
export default function DeviceConfig({ tick, onSelect }: { tick: number; onSelect: (uuid: string) => void }) {
  const { schema, error } = useSchema();
  const [uuid, setUuid] = useState<string | null>(uuidFromHash);

  useEffect(() => {
    const on = () => setUuid(uuidFromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  return (
    <div className="explorer cfgx">
      <div className="col">
        <DevicePicker selected={uuid} tick={tick} />
      </div>
      <div className="cfgr">
        {error && <Card title="운전 설정" className="full"><div className="err">항목 정의(GET /api/settings/schema)를 못 읽었다 — {error}</div></Card>}
        {!error && !schema && <Card title="운전 설정" className="full"><div className="muted">항목 정의 불러오는 중…</div></Card>}
        {schema && !uuid && (
          <Card title="운전 설정" className="full" meta={`ui_items ${schema.version}`}>
            <div className="ph">
              <b>왼쪽에서 단말을 고른다</b>
              <span>서버는 새 단말의 운전 설정을 모른다 — 고른 뒤 "단말에서 읽기"로 25개 값과 1년 스케줄 조건을 받아 온다(읽고 나서 쓴다).</span>
            </div>
          </Card>
        )}
        {schema && uuid && <SettingsPanel key={uuid} uuid={uuid} schema={schema} onSelect={onSelect} />}
      </div>
    </div>
  );
}
