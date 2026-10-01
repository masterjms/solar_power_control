// 서버 설정(#server) — 운영 중 최고관리자가 바꾸는 값(문제점 14번, ADR-012). 모든 단말에 똑같이 적용된다.
// 항목·범위·기본값은 서버가 준다(GET /api/server-settings) — 항목이 늘어도 이 화면은 그대로다.
import { useEffect, useState } from "react";
import { api, ApiErrorException, ServerSettings as Data, errorText } from "./api";
import { resetCommandWait } from "./Command";
import { localTime } from "./format";
import { Card } from "./ui";

export default function ServerSettings({ role }: { role: string | null }) {
  const [data, setData] = useState<Data | null>(null);
  const [edit, setEdit] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fill = (d: Data) => {
    setData(d);
    setEdit(Object.fromEntries(d.groups.flatMap((g) => g.items.map((i) => [i.key, String(i.value)]))));
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
  const num = (k: string) => Number(edit[k]);
  const bad = (k: string) => {
    const it = items.find((i) => i.key === k)!;
    const v = num(k);
    return (edit[k] ?? "").trim() === "" || !Number.isInteger(v) || v < it.min || v > it.max;
  };
  const changed = items.filter((i) => num(i.key) !== i.value);
  const anyBad = items.some((i) => bad(i.key));

  async function save() {
    setErr(null);
    setMsg(null);
    setFieldErr({});
    setBusy(true);
    try {
      fill(await api.putServerSettings(Object.fromEntries(changed.map((i) => [i.key, num(i.key)]))));
      resetCommandWait();
      setMsg("저장했습니다. 지금부터 보내는 명령에 적용됩니다.");
    } catch (e) {
      const f = e instanceof ApiErrorException ? (e.err.detail as { fields?: Record<string, string> } | undefined)?.fields : undefined;
      if (f) setFieldErr(f);
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  // 원격 명령 묶음의 요약: 기다리는 시간 × 보내는 횟수 = 실패로 끝나는 시간
  const w = num("command_wait_sec");
  const n = num("command_attempts");

  return (
    <div className="content">
      {data.groups.map((g) => (
        <Card key={g.id} title={g.title} className="full" meta="모든 단말에 똑같이 적용 · 최고관리자만 변경">
          {g.items.map((it) => (
            <div key={it.key} className={`fld srv ${bad(it.key) || fieldErr[it.key] ? "bad" : ""}`} title={`${it.min}~${it.max}${it.unit} · 기본 ${it.default}${it.unit}`}>
              <label>{num(it.key) !== it.value && <span className="mk edit" title="바꿨지만 아직 저장 안 함" />}{it.label}</label>
              <div className="in">
                <input type="range" min={it.min} max={it.max} step={1} value={Number.isFinite(num(it.key)) ? num(it.key) : it.min} disabled={busy}
                  aria-label={`${it.label} 선택바`} onChange={(e) => setEdit((x) => ({ ...x, [it.key]: e.target.value }))} />
              </div>
              <input className="num" type="number" min={it.min} max={it.max} step={1} value={edit[it.key] ?? ""} disabled={busy} aria-label={it.label}
                onChange={(e) => setEdit((x) => ({ ...x, [it.key]: e.target.value }))} />
              <span className="unit">{it.unit}</span>
              <div className="help">
                {it.help} ({it.min}~{it.max}{it.unit}, 기본 {it.default}{it.unit})
                {it.updated_by ? ` · 마지막 변경 ${it.updated_by} ${localTime(it.updated_at)}` : ""}
              </div>
              {(bad(it.key) || fieldErr[it.key]) && <div className="help c-alarm">{fieldErr[it.key] ?? `${it.min}~${it.max}${it.unit} 사이 정수`}</div>}
            </div>
          ))}
          {g.id === "command" && !anyBad && (
            <div className="cap" style={{ marginTop: 8 }}>
              지금 값이면: 명령을 보내고 {w}초 동안 응답이 없으면 {n > 1 ? `다시 보내고(모두 ${n}회)` : "다시 보내지 않고"}, <b>{w * n}초</b> 뒤에도
              응답이 없으면 "무응답(실패)"으로 끝납니다. 늦게 응답이 오면 결과는 그 응답으로 바뀝니다.
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
