// 계정 관리(#accounts) — 최고관리자만(문제점 21번, ADR-013).
// 역할 셋: 최고관리자(최대 3명, .env 포함) · 지역관리자(시·도 여러 개) · 게스트(대시보드 보기). 사용 기간 7일~1년·무기한.
// .env 계정(ADMIN_USER·OPERATOR_USER)은 보이기만 하고 여기서 바꾸지 않는다(서버 .env 에서).
import { FormEvent, useEffect, useState } from "react";
import { Account, AccountList, LoginRecord, Role, api, ApiErrorException, errorText } from "./api";
import { localTime, relTime } from "./format";
import { Card, nf } from "./ui";

const EXPIRY_LABEL: Record<string, string> = {
  "7d": "7일", "15d": "15일", "30d": "30일", "90d": "90일", "180d": "180일", "365d": "1년", never: "무기한",
};
const ROLE_OPTS: [Role, string, string][] = [
  ["region_admin", "지역관리자", "맡은 시·도 안의 단말만 보고 조작한다"],
  ["guest", "게스트", "맡은 시·도의 대시보드만 본다(버튼 없음)"],
  ["super_admin", "최고관리자", "모든 기능(최대 3명)"],
];
const REASON: Record<string, string> = { ok: "성공", bad_password: "비밀번호 틀림", expired: "기간 만료", disabled: "사용 중지" };

function fieldErrors(e: unknown): Record<string, string> {
  return e instanceof ApiErrorException ? ((e.err.detail as { fields?: Record<string, string> } | undefined)?.fields ?? {}) : {};
}

/** 시·도 여러 개 고르기 — 칩을 눌러 켜고 끈다. */
function RegionPicker({ regions, value, onChange }: { regions: AccountList["regions"]; value: number[]; onChange: (v: number[]) => void }) {
  if (!regions.length) return <span className="muted">트리에 시·도가 아직 없다 — 지역(법정동)에서 먼저 추가</span>;
  return (
    <span className="chips">
      {regions.map((r) => {
        const on = value.includes(r.id);
        return (
          <button key={r.id} type="button" className={`chip ${on ? "on" : ""}`} aria-pressed={on}
            onClick={() => onChange(on ? value.filter((x) => x !== r.id) : [...value, r.id])}>{r.name}</button>
        );
      })}
    </span>
  );
}

function AccountForm({ data, onDone }: { data: AccountList; onDone: (msg: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("region_admin");
  const [regions, setRegions] = useState<number[]>([]);
  const [expires, setExpires] = useState("90d");
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const superFull = data.super_count >= data.max_super;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErrs({});
    setErr(null);
    setBusy(true);
    try {
      await api.createAccount({ username: username.trim(), password, role, region_ids: role === "super_admin" ? null : regions, expires });
      onDone(`${username.trim()} 계정을 만들었습니다.`);
      setUsername(""); setPassword(""); setRegions([]);
    } catch (x) {
      setErrs(fieldErrors(x));
      setErr(errorText(x));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form2" onSubmit={submit}>
      <label>아이디 <small>영문·숫자·_·- 3~32자</small>
        <input value={username} autoComplete="off" onChange={(e) => setUsername(e.target.value)} />
        {errs.username && <small className="c-alarm">{errs.username}</small>}</label>
      <label>처음 비밀번호 <small>8자 이상 — 본인이 로그인 뒤 바꾼다</small>
        <input type="password" value={password} autoComplete="new-password" onChange={(e) => setPassword(e.target.value)} />
        {errs.password && <small className="c-alarm">{errs.password}</small>}</label>
      <label className="w2">역할
        <span className="chips">
          {ROLE_OPTS.map(([r, l, h]) => (
            <button key={r} type="button" className={`chip ${role === r ? "on" : ""}`} aria-pressed={role === r} title={h}
              disabled={r === "super_admin" && superFull} onClick={() => setRole(r)}>
              {l}{r === "super_admin" ? ` (${data.super_count}/${data.max_super})` : ""}
            </button>
          ))}
        </span>
        <small>{ROLE_OPTS.find((o) => o[0] === role)?.[2]}</small>
        {errs.role && <small className="c-alarm">{errs.role}</small>}</label>
      {role !== "super_admin" && (
        <label className="w2">맡을 시·도 <small>여러 개 고를 수 있다</small>
          <RegionPicker regions={data.regions} value={regions} onChange={setRegions} />
          {errs.region_ids && <small className="c-alarm">{errs.region_ids}</small>}</label>
      )}
      <label className="w2">사용 기간 <small>지나면 로그인이 막힌다(지우지는 않음 — 연장 가능)</small>
        <span className="chips">
          {data.expiry_choices.map((k) => (
            <button key={k} type="button" className={`chip ${expires === k ? "on" : ""}`} aria-pressed={expires === k} onClick={() => setExpires(k)}>{EXPIRY_LABEL[k] ?? k}</button>
          ))}
        </span></label>
      <div className="w2 bar2">
        <button type="submit" className="btn pri" disabled={busy || !username.trim() || !password}>{busy ? "만드는 중…" : "계정 만들기"}</button>
        {err && <span className="err">{err}</span>}
      </div>
    </form>
  );
}

/** 계정 한 줄 편집(역할·시·도·기간 연장·사용 중지·비밀번호 재설정·삭제). */
function AccountEdit({ a, data, onDone, onClose }: { a: Account; data: AccountList; onDone: (msg: string) => void; onClose: () => void }) {
  const [role, setRole] = useState<Role>(a.role);
  const [regions, setRegions] = useState<number[]>(a.region_ids ?? []);
  const [expires, setExpires] = useState<string>("");
  const [pw, setPw] = useState("");
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const run = async (f: () => Promise<unknown>, msg: string) => {
    setErr(null); setErrs({});
    try { await f(); onDone(msg); } catch (x) { setErrs(fieldErrors(x)); setErr(errorText(x)); }
  };
  const superFull = data.super_count >= data.max_super && a.role !== "super_admin";
  return (
    <div className="sec">
      <h4>{a.username} 바꾸기 <span><button type="button" className="btn sm" onClick={onClose}>닫기</button></span></h4>
      <div className="form2">
        <label className="w2">역할
          <span className="chips">
            {ROLE_OPTS.map(([r, l]) => (
              <button key={r} type="button" className={`chip ${role === r ? "on" : ""}`} aria-pressed={role === r}
                disabled={r === "super_admin" && superFull} onClick={() => setRole(r)}>{l}</button>
            ))}
          </span>{errs.role && <small className="c-alarm">{errs.role}</small>}</label>
        {role !== "super_admin" && (
          <label className="w2">맡을 시·도<RegionPicker regions={data.regions} value={regions} onChange={setRegions} />
            {errs.region_ids && <small className="c-alarm">{errs.region_ids}</small>}</label>
        )}
        <label className="w2">사용 기간 다시 정하기 <small>지금부터 · 안 고르면 그대로({a.expires_at ? localTime(a.expires_at) : "무기한"})</small>
          <span className="chips">
            {data.expiry_choices.map((k) => (
              <button key={k} type="button" className={`chip ${expires === k ? "on" : ""}`} aria-pressed={expires === k} onClick={() => setExpires(expires === k ? "" : k)}>{EXPIRY_LABEL[k] ?? k}</button>
            ))}
          </span></label>
        <div className="w2 bar2">
          <button type="button" className="btn pri" onClick={() => run(() => api.patchAccount(a.id!, {
            role, region_ids: role === "super_admin" ? null : regions, ...(expires ? { expires } : {}),
          }), `${a.username} 저장함`)}>저장</button>
          <button type="button" className="btn" onClick={() => run(() => api.patchAccount(a.id!, { disabled: !a.disabled }), `${a.username} ${a.disabled ? "사용 재개" : "사용 중지"}`)}>
            {a.disabled ? "사용 재개" : "사용 중지"}
          </button>
          <span className="sp" />
          <button type="button" className="btn danger" onClick={() => confirm(`${a.username} 계정을 지울까요? 되돌릴 수 없습니다.`) && run(() => api.deleteAccount(a.id!), `${a.username} 지움`)}>삭제</button>
        </div>
        <label className="w2">비밀번호 재설정 <small>8자 이상 — 그 계정의 지금 로그인은 끊긴다</small>
          <span className="bar2" style={{ flexWrap: "nowrap" }}>
            <input type="password" value={pw} autoComplete="new-password" style={{ flex: 1 }} onChange={(e) => setPw(e.target.value)} />
            <button type="button" className="btn" disabled={pw.length < 8} onClick={() => run(() => api.resetPassword(a.id!, pw), `${a.username} 비밀번호 재설정`)}>재설정</button>
          </span></label>
        {err && <div className="w2 err">{err}</div>}
      </div>
    </div>
  );
}

export default function Accounts({ role }: { role: string | null }) {
  const [data, setData] = useState<AccountList | null>(null);
  const [logs, setLogs] = useState<LoginRecord[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [edit, setEdit] = useState<number | null>(null);
  const load = () => Promise.all([api.accounts(), api.loginLog(50)]).then(([d, l]) => (setData(d), setLogs(l), setErr(null))).catch((e) => setErr(errorText(e)));
  useEffect(() => { if (role === "super_admin") load(); }, [role]);

  if (role !== "super_admin")
    return <div className="content"><Card title="계정 관리" className="full"><div className="muted">최고관리자만 볼 수 있습니다.</div></Card></div>;
  const done = (m: string) => { setMsg(m); setEdit(null); load(); };
  const expiring = data?.items.filter((a) => a.expiring).length ?? 0;
  const editing = data?.items.find((a) => a.id !== null && a.id === edit);

  return (
    <div className="content">
      <Card title="계정" className="full" meta={data ? `${nf(data.items.length)}개 · 최고관리자 ${data.super_count}/${data.max_super}${expiring ? ` · 7일 안 만료 ${expiring}` : ""}` : ""}>
        {err && <div className="err">{err}</div>}
        {msg && <div className="okl">{msg}</div>}
        <div className="tw">
          <table className="list">
            <thead><tr><th>아이디</th><th>역할</th><th>맡은 시·도</th><th>사용 기간</th><th>상태</th><th>마지막 로그인</th><th>만든 사람</th><th /></tr></thead>
            <tbody>
              {data?.items.map((a) => (
                <tr key={a.username}>
                  <td>{a.username}</td>
                  <td>{a.role_label}</td>
                  <td>{a.role === "super_admin" || a.role === "admin" ? <span className="muted">전 지역</span> : a.regions.join(", ")}</td>
                  <td className={a.expired ? "c-alarm" : a.expiring ? "c-warn" : ""}>{a.expires_at ? `${localTime(a.expires_at)}${a.expired ? " (만료)" : a.expiring ? " (곧 만료)" : ""}` : "무기한"}</td>
                  <td>{a.source === "env" ? <span className="muted">.env 계정</span> : a.disabled ? <span className="c-alarm">사용 중지</span> : a.expired ? <span className="c-alarm">만료</span> : "사용 중"}</td>
                  <td title={a.last_login_at ?? ""}>{a.last_login_at ? relTime(a.last_login_at) : "-"}</td>
                  <td>{a.created_by ?? "-"}</td>
                  <td>{a.source === "db" && <button type="button" className="btn sm" onClick={() => setEdit(edit === a.id ? null : a.id)}>바꾸기</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data && editing && <AccountEdit key={editing.id!} a={editing} data={data} onDone={done} onClose={() => setEdit(null)} />}
      </Card>
      <Card title="새 계정" className="full" meta="최고관리자만">
        {data && <AccountForm data={data} onDone={done} />}
      </Card>
      <Card title="로그인 기록" className="full" meta="최근 50건 · 1년 보관">
        <div className="tw">
          <table className="list">
            <thead><tr><th>시각</th><th>아이디</th><th>결과</th><th>IP</th></tr></thead>
            <tbody>
              {logs.map((l, i) => (
                <tr key={i}><td>{localTime(l.at)}</td><td>{l.username}</td>
                  <td className={l.ok ? "" : "c-alarm"}>{REASON[l.reason] ?? l.reason}</td><td className="mono">{l.ip ?? "-"}</td></tr>
              ))}
              {logs.length === 0 && <tr><td colSpan={4} className="empty">기록이 없습니다.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
