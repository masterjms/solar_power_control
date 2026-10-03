// 서버 설정(#server) — 운영 중 최고관리자가 바꾸는 값(문제점 14·27·29번, ADR-012). 모든 단말에 똑같이 적용된다.
// 항목·범위·기본값·종류(kind: int|date)·배율(scale)은 서버가 준다(GET /api/server-settings) — 항목이 늘어도 이 화면은 그대로다.
import { useEffect, useState } from "react";
import { api, ApiErrorException, ServerSettingItem, ServerSettings as Data, errorText } from "./api";
import { resetCommandWait } from "./Command";
import { localTime } from "./format";
import { Card } from "./ui";

/** 저장값(정수) → 입력칸 글자. date 는 YYYYMMDD → "YYYY-MM-DD"(0 이면 빈칸), scale 은 나눈 값. */
function toText(it: ServerSettingItem, v: number): string {
  if (it.kind === "date") {
    if (!v) return "";
    const s = String(v).padStart(8, "0");
    return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  }
  return String(v / it.scale);
}

/** 입력칸 글자 → 저장값(정수). 못 바꾸면 NaN. */
function toStored(it: ServerSettingItem, t: string): number {
  if (it.kind === "date") {
    if (t.trim() === "") return 0;
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(t.trim());
    return m ? Number(m[1]) * 10000 + Number(m[2]) * 100 + Number(m[3]) : NaN;
  }
  const n = Number(t);
  return t.trim() === "" || !Number.isFinite(n) ? NaN : Math.round(n * it.scale);
}

const rangeText = (it: ServerSettingItem) =>
  it.kind === "date" ? "날짜 또는 빈칸" : `${it.min / it.scale}~${it.max / it.scale}${it.unit}`;

export default function ServerSettings({ role }: { role: string | null }) {
  const [data, setData] = useState<Data | null>(null);
  const [edit, setEdit] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fill = (d: Data) => {
    setData(d);
    setEdit(Object.fromEntries(d.groups.flatMap((g) => g.items.map((i) => [i.key, toText(i, i.value)]))));
  };
  useEffect(() => {
    if (role !== "super_admin") return;
    api.serverSettings().then(fill).catch((e) => setErr(errorText(e)));
  }, [role]);

  if (role !== "super_admin")
    return <div className="content"><Card title="서버 설정" className="full"><div className="muted">최고관리자만 볼 수 있습니다.</div></Card></div>;
  if (!data)
    return <div className="content"><Card title="서버 설정" className="full">{err ? <div className="err">{err}</div> : <div className="muted">불러오는 중…</div>}</Card></div>;

  const items = data.groups.flatMap((g) => g.items);
  const stored = (it: ServerSettingItem) => toStored(it, edit[it.key] ?? "");
  const bad = (it: ServerSettingItem) => {
    const v = stored(it);
    return !Number.isInteger(v) || v < it.min || v > it.max;
  };
  const changed = items.filter((i) => stored(i) !== i.value);
  const anyBad = items.some(bad);

  async function save() {
    setErr(null);
    setMsg(null);
    setFieldErr({});
    setBusy(true);
    try {
      fill(await api.putServerSettings(Object.fromEntries(changed.map((i) => [i.key, stored(i)]))));
      resetCommandWait();
      setMsg("저장했습니다. 바로 적용됩니다.");
    } catch (e) {
      const f = e instanceof ApiErrorException ? (e.err.detail as { fields?: Record<string, string> } | undefined)?.fields : undefined;
      if (f) setFieldErr(f);
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  /** 통계 다시 시작(문제점 27번) — 시작일 = 오늘, 누적 0, 오늘 전 하루 요약 삭제. */
  async function resetStats() {
    if (!confirm("통계를 오늘부터 다시 시작할까요?\n\n- 통계 시작일이 오늘로 바뀝니다\n- 모든 단말의 누적 발전·사용이 0 이 됩니다\n- 오늘 전의 하루 요약 기록이 지워집니다(10분 보고 원문은 보관 기간대로 따로 지워집니다)\n\n되돌릴 수 없습니다.")) return;
    setErr(null);
    setMsg(null);
    setBusy(true);
    try {
      const r = await api.energyReset();
      fill(await api.serverSettings());
      setMsg(`통계를 ${r.since} 부터 다시 시작합니다 (하루 요약 ${r.daily_rows_deleted}행 삭제, 단말 ${r.devices_zeroed}대 누적 0).`);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  // 원격 명령 묶음의 요약: 기다리는 시간 × 보내는 횟수 = 실패로 끝나는 시간
  const w = Number(edit.command_wait_sec);
  const n = Number(edit.command_attempts);

  return (
    <div className="content">
      {data.groups.map((g) => (
        <Card key={g.id} title={g.title} className="full"
          meta={g.id === "retention" ? "지난 기록은 매일 01:00 에 지운다 · 최고관리자만 변경" : "모든 단말에 똑같이 적용 · 최고관리자만 변경"}>
          {g.items.map((it) => (
            <div key={it.key} className={`fld srv ${bad(it) || fieldErr[it.key] ? "bad" : ""}`} title={`${rangeText(it)} · 기본 ${toText(it, it.default) || "없음"}${it.unit}`}>
              <label>{stored(it) !== it.value && <span className="mk edit" title="바꿨지만 아직 저장 안 함" />}{it.label}</label>
              <div className="in">
                {it.kind === "date" ? (
                  <input className="inp" type="date" value={edit[it.key] ?? ""} disabled={busy} aria-label={it.label}
                    onChange={(e) => setEdit((x) => ({ ...x, [it.key]: e.target.value }))} />
                ) : (
                  <input type="range" min={it.min / it.scale} max={it.max / it.scale} step={1 / it.scale}
                    value={Number.isFinite(Number(edit[it.key])) ? Number(edit[it.key]) : it.min / it.scale} disabled={busy}
                    aria-label={`${it.label} 선택바`} onChange={(e) => setEdit((x) => ({ ...x, [it.key]: e.target.value }))} />
                )}
              </div>
              {it.kind === "date" ? (
                <button type="button" className="btn sm" disabled={busy || !edit[it.key]} onClick={() => setEdit((x) => ({ ...x, [it.key]: "" }))}>비우기</button>
              ) : (
                <input className="num" type="number" min={it.min / it.scale} max={it.max / it.scale} step={1 / it.scale} value={edit[it.key] ?? ""} disabled={busy} aria-label={it.label}
                  onChange={(e) => setEdit((x) => ({ ...x, [it.key]: e.target.value }))} />
              )}
              <span className="unit">{it.unit}</span>
              <div className="help">
                {it.help} ({rangeText(it)}, 기본 {toText(it, it.default) || "없음"}{it.unit})
                {it.updated_by ? ` · 마지막 변경 ${it.updated_by} ${localTime(it.updated_at)}` : ""}
              </div>
              {(bad(it) || fieldErr[it.key]) && <div className="help c-alarm">{fieldErr[it.key] ?? `${rangeText(it)} 사이`}</div>}
            </div>
          ))}
          {g.id === "command" && !anyBad && (
            <div className="cap" style={{ marginTop: 8 }}>
              지금 값이면: 명령을 보내고 {w}초 동안 응답이 없으면 {n > 1 ? `다시 보내고(모두 ${n}회)` : "다시 보내지 않고"}, <b>{w * n}초</b> 뒤에도
              응답이 없으면 "무응답(실패)"으로 끝납니다. 늦게 응답이 오면 결과는 그 응답으로 바뀝니다.
            </div>
          )}
          {g.id === "energy" && (
            <div className="bar2" style={{ marginTop: 8 }}>
              <button type="button" className="btn danger" disabled={busy} onClick={resetStats}>통계 다시 시작 (오늘부터)</button>
              <span className="cap">시험을 끝내고 운영을 시작할 때 누릅니다 — 시작일이 오늘이 되고 누적이 0 부터 다시 쌓입니다.</span>
            </div>
          )}
        </Card>
      ))}
      <Card className="full" title="저장">
        <div className="bar2">
          <button type="button" className="btn pri" disabled={busy || anyBad || changed.length === 0} onClick={save}>
            {busy ? "저장 중…" : changed.length ? `바꾼 ${changed.length}개 저장` : "바뀐 값 없음"}
          </button>
          <button type="button" className="btn" disabled={busy || changed.length === 0} onClick={() => (fill(data), setFieldErr({}), setErr(null))}>되돌리기</button>
          {msg && <span className="okl">{msg}</span>}
          {err && <span className="err">{err}</span>}
        </div>
      </Card>
    </div>
  );
}
