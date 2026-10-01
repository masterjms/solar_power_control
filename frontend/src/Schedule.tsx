// 스케줄 배포(#schedule) — S-25, 사양서 §13.1, ADR-010.
// ① 프로필 등록(전국 시·군 → 지역·대표 좌표(문제점 18번), 보정, 운전 15개, 미리보기) ② 법정동 트리 배정(단말 예외) ③ 배포 진행 표
// ④ ACK·crc 로 "적용됨". 메시지는 S-23 SETTINGS_SET(25개 + tbl) 그대로 — 25개 = 단말의 마지막 읽은 값 + 프로필 15개.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, DeployJob, DeviceSchedule, ScheduleAssign, ScheduleProfile, SchedulePreview, SettingsItem,
  SettingsSchema, SettingsTbl, errorText,
} from "./api";
import { CitySelect } from "./CitySelect";
import { FieldCtx, GroupFields, StageBar, useSchema } from "./DeviceConfig";
import { localTime } from "./format";
import { parseText, toText, rangeError, utf8Bytes, hoursText } from "./settingsLogic";
import { RegionTree, TreeNode, useRegions } from "./Tree";
import { Card, Met, OnlineMark, nf } from "./ui";

const REFRESH_MS = 10_000;
const PROFILE_GROUPS = ["schedule", "stage"];
const REGION_MAX = 47;

export const DEPLOY_STATUS: Record<string, [string, string]> = {
  waiting: ["대기(오프라인·차례)", "b-off"],
  reading: ["단말에서 읽는 중", "b-blue"],
  sent: ["발행함 — 응답 대기", "b-blue"],
  OK: ["적용됨", "b-ok"],
  CRC: ["CRC 불일치", "b-alarm"],
  RULE: ["규칙 위반", "b-alarm"],
  RANGE: ["범위 밖", "b-alarm"],
  BAD: ["형식 오류", "b-alarm"],
  STATE: ["승인 안 됨", "b-warn"],
  FLASH: ["Flash 실패", "b-alarm"],
  NO_RESPONSE: ["응답 없음", "b-alarm"],
  READ_FAILED: ["읽기 실패", "b-alarm"],
  CANCELLED: ["취소", "b-off"],
  SUPERSEDED: ["새로 보낸 것으로 대체", "b-off"],
};
export function DeployBadge({ s }: { s: string }) {
  const [label, cls] = DEPLOY_STATUS[s] ?? [s, "b-off"];
  return <span className={`badge ${cls}`} title={s}>{label}</span>;
}

interface Form {
  name: string;
  region: string;
  lat: string;
  lon: string;
  on: string;
  off: string;
  address: string;
  edit: Record<string, string>;
}

function formOf(p: ScheduleProfile | null, items: SettingsItem[], defaults: Record<string, number>): Form {
  const vals = p ? p.values : defaults;
  const edit: Record<string, string> = {};
  items.forEach((it) => (edit[it.key] = toText(it, vals[it.key])));
  return {
    name: p?.name ?? "", region: p?.region ?? "", lat: p ? String(p.lat_e6 / 1e6) : "", lon: p ? String(p.lon_e6 / 1e6) : "",
    on: String(p?.on ?? 0), off: String(p?.off ?? 0), address: p?.address ?? "", edit,
  };
}

// ---------------- 프로필 편집 ----------------
function ProfileEditor({ schema, profile, defaults, onSaved, onDeleted }: {
  schema: SettingsSchema; profile: ScheduleProfile | null; defaults: Record<string, number>;
  onSaved: (p: ScheduleProfile) => void; onDeleted: () => void;
}) {
  const groups = schema.groups.filter((g) => PROFILE_GROUPS.includes(g.id));
  const items = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  const [f, setF] = useState<Form>(() => formOf(profile, items, defaults));
  const [prev, setPrev] = useState<SchedulePreview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setF(formOf(profile, items, defaults));
    setPrev(null);
    setMsg(null);
    setErr(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile?.id, profile?.version]);

  const values: Record<string, number | null> = {};
  items.forEach((it) => (values[it.key] = parseText(it, f.edit[it.key])));
  const bad = new Set(items.filter((it) => rangeError(it, values[it.key])).map((i) => i.key));
  const saved = profile ? profile.values : null;
  const ctx: FieldCtx = {
    schema, edit: f.edit, values: (saved ?? defaults) as Record<string, number>, disabled: busy, bad,
    serverErr: {}, set: (k, t) => setF((x) => ({ ...x, edit: { ...x.edit, [k]: t } })),
  };
  const regionBytes = utf8Bytes(f.region.trim());
  const pv = (k: string) => values[k] ?? null;
  const lat = Number(f.lat), lon = Number(f.lon), on = Number(f.on), off = Number(f.off);
  const condOk = f.lat.trim() !== "" && f.lon.trim() !== "" && Number.isFinite(lat) && Number.isFinite(lon)
    && Number.isInteger(on) && Number.isInteger(off) && Math.abs(on) <= 180 && Math.abs(off) <= 180;
  const pseudoTbl: SettingsTbl | null = condOk
    ? { region: f.region, lat_e6: Math.round(lat * 1e6), lon_e6: Math.round(lon * 1e6), on, off, src: 2, ss: 0, crc: "", crc_expected: null, matches: null }
    : null;

  async function preview() {
    setErr(null);
    if (!condOk) return setErr("좌표·보정을 먼저 넣는다");
    try {
      setPrev(await api.schedulePreview({ lat, lon, on, off }));
    } catch (e) {
      setErr(errorText(e));
    }
  }

  async function save() {
    setErr(null);
    setMsg(null);
    if (!f.name.trim()) return setErr("이름을 넣는다");
    if (!f.region.trim() || regionBytes > REGION_MAX) return setErr(`지역명은 1~${REGION_MAX}B`);
    if (!condOk) return setErr("좌표·보정(정수 분 -180~180)을 확인한다");
    if (bad.size) return setErr("범위를 벗어난 항목이 있다");
    const body = {
      name: f.name.trim(), region: f.region.trim(), lat, lon, on, off, address: f.address || null,
      values: Object.fromEntries(items.map((it) => [it.key, values[it.key] as number])),
    };
    setBusy(true);
    try {
      const p = profile ? await api.patchScheduleProfile(profile.id, body) : await api.createScheduleProfile(body);
      setMsg(`저장함 — v${p.version} · crc ${p.crc}${profile && p.version !== profile.version ? " (판이 올라갔다 — 적용된 단말은 다시 보내야 한다)" : ""}`);
      onSaved(p);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!profile || !confirm(`${profile.name} 양식을 지울까요? (지정된 곳이 있으면 지워지지 않는다)`)) return;
    try {
      await api.deleteScheduleProfile(profile.id);
      onDeleted();
    } catch (e) {
      setErr(errorText(e));
    }
  }

  return (
    <Card className="full" title={profile ? `양식 · ${profile.name}` : "새 스케줄 양식"}
      meta={profile ? <span className="mono">v{profile.version} · crc {profile.crc}</span> : "조건 + 운전 15개"}>
      <div className="form2">
        <label>이름<input value={f.name} maxLength={60} placeholder="예: 경기 남부 기본" onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <label>지역 <small>전국 시·군에서 고르면 대표 좌표가 채워진다</small>
          <CitySelect value={f.region} onPick={(c) => (setF({ ...f, region: c.label, lat: String(c.lat), lon: String(c.lon), address: "" }), setPrev(null))} />
        </label>
      </div>
      <div className="form5">
        <label><span>지역명 · <b className={regionBytes > REGION_MAX ? "c-alarm" : ""}>{regionBytes}/{REGION_MAX}B</b></span>
          <input className="inp" value={f.region} readOnly placeholder="위에서 고른다" /></label>
        <label><span>위도</span><input className="inp" value={f.lat} placeholder="37.36" onChange={(e) => (setF({ ...f, lat: e.target.value }), setPrev(null))} /></label>
        <label><span>경도</span><input className="inp" value={f.lon} placeholder="126.93" onChange={(e) => (setF({ ...f, lon: e.target.value }), setPrev(null))} /></label>
        <label><span>점등 보정 (분)</span><input className="inp" type="number" min={-180} max={180} value={f.on} onChange={(e) => (setF({ ...f, on: e.target.value }), setPrev(null))} /></label>
        <label><span>소등 보정 (분)</span><input className="inp" type="number" min={-180} max={180} value={f.off} onChange={(e) => (setF({ ...f, off: e.target.value }), setPrev(null))} /></label>
      </div>
      {f.address && <div className="cap">주소: {f.address}</div>}
      <div className="grp2">
        {groups.map((g) => (
          <Card key={g.id} title={g.title} meta={`${g.items.length}개`}>
            <GroupFields c={ctx} items={g.items} />
            {g.id === "stage" && <StageBar schema={schema} pv={pv} tbl={pseudoTbl} />}
          </Card>
        ))}
      </div>
      <div className="cap">양식에 넣지 않는 10개(기준 밝기 PWM1~3·Fade·배터리 6개)는 단말마다 다르다 — 보낼 때 그 단말에서 마지막으로 읽은 값을 그대로 보낸다.</div>
      <div className="bar2">
        <button type="button" className="btn pri" disabled={busy} onClick={save}>{profile ? "저장" : "만들기"}</button>
        <button type="button" className="btn" onClick={preview} disabled={!condOk}>미리보기</button>
        {profile && <button type="button" className="btn danger" onClick={remove}>삭제</button>}
        <span className="sp" />
        {profile && <span className="cap">지정 지역 {nf(profile.assigned_nodes)} · 단말 예외 {nf(profile.assigned_devices)} · 대상 {nf(profile.targets)}대 · 적용됨 {nf(profile.applied)}대</span>}
      </div>
      {msg && <div className="okl">{msg}</div>}
      {err && <div className="err">{err}</div>}
      {prev && (
        <>
          <div className="cap">미리보기 표 CRC <span className="mono">{prev.crc}</span> — 단말이 같은 식으로 계산해 이 CRC 와 같을 때만 저장한다.</div>
          <div style={{ maxHeight: 280, overflow: "auto" }}>
            <table className="mini">
              <thead><tr><th>날짜</th><th>점등</th><th>소등</th><th className="n">점등 시간</th></tr></thead>
              <tbody>{prev.rows.map((r) => <tr key={`${r.month}-${r.day}`}><td>{r.month}월 {r.day}일</td><td>{r.on}</td><td>{r.off}</td><td className="n">{hoursText(r.hours)}</td></tr>)}</tbody>
            </table>
          </div>
        </>
      )}
    </Card>
  );
}

// ---------------- 배포 작업 ----------------
function JobDetail({ id, onClose, onSelect }: { id: number; onClose: () => void; onSelect: (u: string) => void }) {
  const [job, setJob] = useState<DeployJob | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => api.deployJob(id).then((j) => (setJob(j), setErr(null))).catch((e) => setErr(errorText(e))), [id]);
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);
  if (!job) return <div className="muted">{err ?? "불러오는 중…"}</div>;
  const retryable = (job.items ?? []).filter((i) => ["NO_RESPONSE", "READ_FAILED", "FLASH", "CRC", "STATE", "RULE", "RANGE", "BAD"].includes(i.status)).length;
  const open = (job.counts.waiting ?? 0) + (job.counts.reading ?? 0) + (job.counts.sent ?? 0);
  return (
    <div className="cmdres">
      <div className="bar2">
        <b>보낸 기록 #{job.id}</b> <span>{job.profile_name} v{job.profile_version} · {job.scope_label ?? job.scope_kind}</span>
        <span className="sp" />
        <button type="button" className="btn sm" disabled={retryable === 0} onClick={async () => {
          try { const r = await api.retryDeploy(job.id); setMsg(`다시 보냄 ${r.retried}대${r.skipped ? ` · 횟수 다 써서 건너뜀 ${r.skipped}대` : ""}`); load(); } catch (e) { setErr(errorText(e)); }
        }}>응답 없는 단말만 다시 ({retryable})</button>
        <button type="button" className="btn sm danger" disabled={open === 0} onClick={async () => {
          if (!confirm("대기·진행 중인 항목을 취소할까요? 이미 보낸 요청은 단말이 적용할 수 있습니다.")) return;
          try { await api.cancelDeploy(job.id); load(); } catch (e) { setErr(errorText(e)); }
        }}>취소</button>
        <button type="button" className="btn sm" onClick={onClose}>닫기</button>
      </div>
      {msg && <div className="okl">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <div className="cap">대기 = 오프라인이면 다시 붙을 때 자동, 온라인이면 차례(초당 10대). 30초 무응답이면 새 명령 번호로 3회까지 — 그래도 없으면 "응답 없음".</div>
      <div style={{ maxHeight: 360, overflow: "auto" }}>
        <table className="mini">
          <thead><tr><th>단말</th><th>통신</th><th>상태</th><th className="n">회차</th><th>보냄</th><th>응답</th><th>설명</th></tr></thead>
          <tbody>
            {(job.items ?? []).map((i) => (
              <tr key={i.uuid} data-click onClick={() => onSelect(i.uuid)}>
                <td>{i.site ?? "-"} <small className="mono muted">{i.uuid.slice(-8)}</small></td>
                <td><OnlineMark on={i.is_online} /></td>
                <td><DeployBadge s={i.status} /></td>
                <td className="n">{i.rounds}</td>
                <td>{localTime(i.sent_at)}</td>
                <td>{localTime(i.acked_at)}</td>
                <td className="wrap">{i.detail ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function jobSummary(j: DeployJob): string {
  const c = j.counts;
  const open = (c.waiting ?? 0) + (c.reading ?? 0) + (c.sent ?? 0);
  const fail = j.total - open - (c.OK ?? 0) - (c.CANCELLED ?? 0) - (c.SUPERSEDED ?? 0);
  return `적용 ${c.OK ?? 0} / ${j.total}${open ? ` · 진행 ${open}` : ""}${fail > 0 ? ` · 실패 ${fail}` : ""}`;
}

// ---------------- 페이지 ----------------
export default function Schedule({ tick, onSelect }: { tick: number; onSelect: (uuid: string) => void }) {
  const { schema, error: schemaErr } = useSchema();
  const [profiles, setProfiles] = useState<ScheduleProfile[] | null>(null);
  const [assigns, setAssigns] = useState<ScheduleAssign[]>([]);
  const [keys, setKeys] = useState<{ keys: string[]; defaults: Record<string, number> } | null>(null);
  const [sel, setSel] = useState<number | "new" | null>(null);
  const [node, setNode] = useState<number | null>(null);
  const [devs, setDevs] = useState<DeviceSchedule[] | null>(null);
  const [jobs, setJobs] = useState<DeployJob[]>([]);
  const [job, setJob] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [local, setLocal] = useState(0);
  const [filter, setFilter] = useState("");
  const [exUuid, setExUuid] = useState("");
  const { tree } = useRegions(tick + local);

  useEffect(() => {
    api.scheduleKeys().then(setKeys).catch((e) => setErr(errorText(e)));
  }, []);
  useEffect(() => {
    let alive = true;
    const load = () =>
      Promise.all([api.scheduleProfiles(), api.scheduleAssigns(), api.deployJobs({ limit: 20 })])
        .then(([p, a, j]) => {
          if (!alive) return;
          setProfiles(p);
          setAssigns(a);
          setJobs(j);
          setSel((s) => (s === null && p.length ? p[0].id : s));
        })
        .catch((e) => alive && setErr(errorText(e)));
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [tick, local]);

  const profile = typeof sel === "number" ? profiles?.find((p) => p.id === sel) ?? null : null;
  const nodeAssign = useMemo(() => new Map(assigns.filter((a) => a.node_id !== null).map((a) => [a.node_id as number, a])), [assigns]);
  const devAssigns = assigns.filter((a) => a.uuid !== null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.scheduleDevices(node !== null ? { node_id: node } : profile ? { profile_id: profile.id } : {})
        .then((r) => alive && setDevs(r.items))
        .catch((e) => alive && setErr(errorText(e)));
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [node, profile?.id, tick, local]);

  const refresh = () => setLocal((n) => n + 1);
  const inherited = (n: TreeNode): ScheduleAssign | null => {
    for (let x: TreeNode | null = n; x; x = x.parent) {
      const a = nodeAssign.get(x.r.id);
      if (a) return a;
    }
    return null;
  };
  const nodeObj = node !== null ? tree.byId.get(node) ?? null : null;
  const nodeOwn = node !== null ? nodeAssign.get(node) ?? null : null;
  const nodeInh = nodeObj ? inherited(nodeObj) : null;

  async function run(fn: () => Promise<string | void>) {
    setErr(null);
    setMsg(null);
    try {
      const m = await fn();
      if (m) setMsg(m);
      refresh();
    } catch (e) {
      setErr(errorText(e));
    }
  }

  const deploy = (scope: "profile" | "node" | "device", scopeId: string | null, label: string) =>
    run(async () => {
      if (!profile) return;
      if (!confirm(`${profile.name} v${profile.version} 을(를) ${label}에 보냅니다.\n단말마다 SETTINGS_SET(25개 + 표 조건)을 보냅니다. 오프라인 단말은 다시 붙으면 자동으로 갑니다.`)) return;
      const j = await api.createDeploy({ profile_id: profile.id, scope, scope_id: scopeId });
      setJob(j.id);
      return `보내기 #${j.id} 시작 — ${j.total}대`;
    });

  const shownDevs = (devs ?? []).filter((d) => node !== null || !profile || d.profile_id === profile.id);

  return (
    <div className="explorer cfgx">
      <div className="col">
        <Card title="스케줄 양식" meta={<button type="button" className="btn sm" onClick={() => setSel("new")}>새 양식</button>}>
          <div className="tree tall" role="listbox" aria-label="스케줄 양식">
            {(profiles ?? []).map((p) => (
              <div key={p.id} className={`tn ${sel === p.id ? "on" : ""}`} role="option" aria-selected={sel === p.id} tabIndex={0}
                onClick={() => setSel(p.id)} onKeyDown={(e) => e.key === "Enter" && setSel(p.id)}>
                <span className="tnm">{p.name}<small>v{p.version} · {p.region}</small></span>
                <span className={`tct ${p.targets && p.applied === p.targets ? "c-ok" : p.targets ? "c-warn" : ""}`}>{nf(p.applied)}/{nf(p.targets)}</span>
              </div>
            ))}
            {profiles && profiles.length === 0 && <div className="tempty">양식이 없습니다. "새 양식"으로 만든다.</div>}
          </div>
        </Card>
        <Card title="지역별 지정" meta="지역에 걸면 아래가 물려받는다">
          <input type="search" placeholder="법정동 찾기" aria-label="트리 필터" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <RegionTree tree={tree} selected={node} onSelect={(v) => setNode(typeof v === "number" ? (v === node ? null : v) : null)} filter={filter} className="tall"
            badge={(n) => {
              const a = nodeAssign.get(n.r.id);
              return a ? <em className="c-blue" title="이 지역에 직접 지정"> ◆ {a.profile_name}</em> : null;
            }} />
        </Card>
      </div>
      <div className="cfgr">
        {schemaErr && <Card title="스케줄" className="full"><div className="err">{schemaErr}</div></Card>}
        {err && <div className="err full">{err}</div>}
        {msg && <div className="okl full">{msg}</div>}
        {schema && keys && (sel === "new" || profile) && (
          <ProfileEditor schema={schema} profile={profile} defaults={keys.defaults}
            onSaved={(p) => (setSel(p.id), refresh())} onDeleted={() => (setSel(null), refresh())} />
        )}

        <Card className="full" title={nodeObj ? `지정 · ${nodeObj.r.name}` : "지정"} meta={nodeObj ? "선택한 지역" : "왼쪽 트리에서 지역을 고른다"}>
          {nodeObj ? (
            <>
              <div className="grid2">
                <Met l="이 지역에 직접" v={nodeOwn ? nodeOwn.profile_name : "없음"} h={nodeOwn ? `${localTime(nodeOwn.assigned_at)} · ${nodeOwn.assigned_by ?? ""}` : ""} />
                <Met l="적용되는 양식(상속 포함)" v={nodeInh ? nodeInh.profile_name : "없음"} h={nodeInh && nodeInh !== nodeOwn ? `위 지역에서 물려받음 — ${nodeInh.label}` : ""} />
              </div>
              <div className="bar2" style={{ marginTop: 8 }}>
                <button type="button" className="btn pri" disabled={!profile} onClick={() => run(async () => {
                  await api.setScheduleAssign({ node_id: node!, profile_id: profile!.id });
                  return `${nodeObj.r.name} ← ${profile!.name} 지정(보내기는 따로)`;
                })}>{profile ? `이 지역에 "${profile.name}" 지정` : "왼쪽에서 양식을 고른다"}</button>
                {nodeOwn && <button type="button" className="btn" onClick={() => run(async () => {
                  await api.setScheduleAssign({ node_id: node!, profile_id: null });
                  return "지정 해제";
                })}>직접 지정 해제</button>}
                <span className="sp" />
                {profile && <button type="button" className="btn" onClick={() => deploy("node", String(node), nodeObj.r.name)}>이 지역 아래 단말에 "{profile.name}" 보내기</button>}
              </div>
            </>
          ) : <div className="cap">지역을 고르면 그 아래 단말의 지정·적용 상태가 보인다. 트리의 ◆ = 직접 지정된 양식.</div>}
          <h4 style={{ marginTop: 12 }}>단말 예외 <span>단말 하나만 다른 양식(지역 지정보다 우선)</span></h4>
          <div className="bar2">
            <input value={exUuid} placeholder="UUID 24자리" style={{ width: 240 }} onChange={(e) => setExUuid(e.target.value)} />
            <button type="button" className="btn" disabled={!profile || exUuid.trim().length !== 24} onClick={() => run(async () => {
              await api.setScheduleAssign({ uuid: exUuid.trim().toUpperCase(), profile_id: profile!.id });
              setExUuid("");
              return "단말 예외 지정";
            })}>{profile ? `"${profile.name}" 예외 지정` : "양식을 고른다"}</button>
          </div>
          {devAssigns.length > 0 && (
            <table className="mini" style={{ marginTop: 8 }}>
              <thead><tr><th>단말</th><th>양식</th><th>지정</th><th /></tr></thead>
              <tbody>{devAssigns.map((a) => (
                <tr key={a.id}><td>{a.label}</td><td>{a.profile_name}</td><td>{localTime(a.assigned_at)}</td>
                  <td><button type="button" className="btn sm" onClick={() => run(async () => { await api.setScheduleAssign({ uuid: a.uuid!, profile_id: null }); return "예외 해제"; })}>해제</button></td></tr>
              ))}</tbody>
            </table>
          )}
        </Card>

        <Card className="full" title="적용 현황" meta={<>
          <span>{nodeObj ? `${nodeObj.r.name} 아래` : profile ? `"${profile.name}" 대상` : "운영 단말"} {nf(shownDevs.length)}대 · 적용됨 {nf(shownDevs.filter((d) => d.applied_ok).length)}</span>
          {profile && <button type="button" className="btn sm pri" onClick={() => deploy("profile", null, `"${profile.name}" 가 지정된 단말 전부`)}>"{profile.name}" 전체 보내기</button>}
        </>}>
          <div style={{ maxHeight: 420, overflow: "auto" }}>
            <table className="mini">
              <thead><tr><th>단말</th><th>통신</th><th>지정 양식</th><th>적용</th><th>단말 표</th><th>진행</th><th>오늘 점등~소등</th><th>DIP4</th><th /></tr></thead>
              <tbody>
                {shownDevs.map((d) => (
                  <tr key={d.uuid}>
                    <td data-click onClick={() => onSelect(d.uuid)}>{d.site ?? "-"} <small className="mono muted">{d.uuid.slice(-8)}</small></td>
                    <td><OnlineMark on={d.is_online} /></td>
                    <td>{d.profile_name ? <>{d.profile_name} <small className="muted">v{d.profile_version} {d.source === "device" ? "· 예외" : ""}</small></> : <span className="muted">지정 없음</span>}</td>
                    <td>{d.profile_id ? (d.applied_ok ? <span className="badge b-ok">적용됨</span> : d.applied_crc ? <span className="badge b-warn" title={`적용된 판 v${d.applied_version} ${d.applied_crc}`}>옛 판·단말과 다름</span> : <span className="badge b-off">미적용</span>) : ""}</td>
                    <td className="mono" title={d.device_region ?? ""}>{d.device_crc ?? <span className="muted">모름</span>}{d.profile_crc && d.device_crc ? (d.device_crc === d.profile_crc ? " ✓" : " ✗") : ""}</td>
                    <td>{d.deploy_status ? <DeployBadge s={d.deploy_status} /> : ""}</td>
                    <td>{d.today_on ? `${d.today_on} ~ ${d.today_off}` : ""}</td>
                    <td>{d.dip4 === null ? "" : d.dip4 ? "ON" : <span className="c-warn" title="DIP4 OFF: 단계 무시, 시작 밝기로만 운전">OFF</span>}</td>
                    <td>{profile && d.profile_id === profile.id && <button type="button" className="btn sm" onClick={() => deploy("device", d.uuid, d.site ?? d.uuid)}>다시 보내기</button>}</td>
                  </tr>
                ))}
                {devs && shownDevs.length === 0 && <tr><td colSpan={9} className="muted">단말이 없습니다.</td></tr>}
              </tbody>
            </table>
          </div>
        </Card>

        <Card className="full" title="보낸 기록" meta="최근 20건 · 3초마다 갱신(열어 둔 기록)">
          {job !== null && <JobDetail id={job} onClose={() => setJob(null)} onSelect={onSelect} />}
          <table className="mini" style={{ marginTop: 8 }}>
            <thead><tr><th>#</th><th>양식</th><th>범위</th><th>진행</th><th>만든 사람·시각</th><th>끝</th></tr></thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} data-click className={job === j.id ? "sel" : ""} onClick={() => setJob(j.id)}>
                  <td>{j.id}</td><td>{j.profile_name} v{j.profile_version}</td><td>{j.scope_label ?? j.scope_kind}</td>
                  <td>{jobSummary(j)}</td><td>{j.created_by ?? ""} · {localTime(j.created_at)}</td>
                  <td>{j.cancelled_at ? "취소" : j.finished_at ? localTime(j.finished_at) : <span className="c-blue">진행 중</span>}</td>
                </tr>
              ))}
              {jobs.length === 0 && <tr><td colSpan={6} className="muted">보낸 적이 없습니다.</td></tr>}
            </tbody>
          </table>
        </Card>
      </div>
    </div>
  );
}
