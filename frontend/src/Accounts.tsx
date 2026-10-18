// 계정 관리(#accounts) — 최고관리자만(문제점 21번, ADR-013).
// 역할 셋: 최고관리자(최대 3명, .env 포함) · 지역관리자(시·도 여러 개) · 게스트(대시보드 보기). 사용 기간 7일~1년·무기한.
// .env 계정(ADMIN_USER·OPERATOR_USER)은 보이기만 하고 여기서 바꾸지 않는다(서버 .env 에서).
import { FormEvent, useEffect, useState } from "react";
import { Account, AccountList, LoginRecord, Role, api, ApiErrorException, errorText, userMessage } from "./api";
import { localTime, relTime } from "./format";
import { passwordProblem } from "./Login";
import { Card, DEFAULT_PAGE_SIZE, PageSize, Pager, nf, pageCount } from "./ui";

const EXPIRY_LABEL: Record<string, string> = {
  "7d": "7일", "15d": "15일", "30d": "30일", "90d": "90일", "180d": "180일", "365d": "1년", never: "무기한",
};
const ROLE_OPTS: [Role, string, string][] = [
  ["region_admin", "지역관리자", "맡은 시·도 안의 단말만 보고 조작합니다"],
  ["guest", "게스트", "맡은 시·도의 대시보드만 봅니다(버튼 없음)"],
  ["super_admin", "최고관리자", "모든 기능(최대 3명)"],
];
const REASON: Record<string, string> = { ok: "성공", bad_password: "비밀번호 틀림", expired: "기간 만료", disabled: "사용 중지",
  locked: "잠긴 동안 시도", unlock: "잠금 해제(최고관리자)" };

function fieldErrors(e: unknown): Record<string, string> {
  return e instanceof ApiErrorException ? ((e.err.detail as { fields?: Record<string, string> } | undefined)?.fields ?? {}) : {};
}

/** 시·도 여러 개 고르기 — 칩을 눌러 켜고 끈다. */
function RegionPicker({ regions, value, onChange }: { regions: AccountList["regions"]; value: number[]; onChange: (v: number[]) => void }) {
  if (!regions.length) return <span className="muted">시·도가 아직 없습니다 — 지역(법정동) 화면에서 먼저 추가하세요</span>;
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
  // 중복 확인 결과(문제점 57번) — 확인한 아이디와 지금 칸의 아이디가 같을 때만 보여 준다
  const [checked, setChecked] = useState<{ name: string; ok: boolean; reason: string | null } | null>(null);
  const superFull = data.super_count >= data.max_super;
  const name = username.trim();
  const check = checked && checked.name === name ? checked : null;

  async function checkName() {
    if (!name) return;
    try {
      const r = await api.checkUsername(name);
      setChecked({ name, ...r });
      setErrs((x) => ({ ...x, username: "" }));
    } catch (x) {
      setChecked({ name, ok: false, reason: userMessage(x) });
    }
  }

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
      const f = fieldErrors(x);
      setErrs(f);
      // 칸별 오류는 칸 아래에 보이므로 아래쪽에는 짧게만(문제점 57번 — 영어 코드·JSON 을 보이지 않는다)
      setErr(Object.keys(f).length ? "빨간 글씨로 표시한 칸을 고쳐 주세요." : userMessage(x));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form2" onSubmit={submit}>
      {/* 두 칸 모두 "제목 한 줄 → 입력 → 안내/오류" 순서라 입력칸 높이가 같다(문제점 57번) */}
      <label>아이디
        <span className="inrow">
          <input value={username} autoComplete="off" onChange={(e) => setUsername(e.target.value)} />
          <button type="button" className="btn" disabled={!name} onClick={checkName}>중복 확인</button>
        </span>
        {errs.username ? <small className="c-alarm">{errs.username}</small>
          : check ? <small className={check.ok ? "c-ok" : "c-alarm"}>{check.ok ? "쓸 수 있는 아이디입니다" : check.reason}</small>
          : <small>영문·숫자·_·- 3~32자</small>}</label>
      <label>처음 비밀번호
        <input type="password" value={password} autoComplete="new-password" onChange={(e) => setPassword(e.target.value)} />
        {errs.password ? <small className="c-alarm">{errs.password}</small>
          : <small>3종류 8자·2종류 10자 이상 — 본인이 처음 로그인하면 바꿔야 합니다</small>}</label>
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
        <label className="w2">맡을 시·도 <small>여러 개 고를 수 있습니다</small>
          <RegionPicker regions={data.regions} value={regions} onChange={setRegions} />
          {errs.region_ids && <small className="c-alarm">{errs.region_ids}</small>}</label>
      )}
      <label className="w2">사용 기간 <small>지나면 로그인이 막힙니다(지우지는 않음 — 연장 가능)</small>
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
    try { await f(); onDone(msg); } catch (x) { const fe = fieldErrors(x); setErrs(fe); setErr(Object.keys(fe).length ? "빨간 글씨로 표시한 칸을 고쳐 주세요." : userMessage(x)); }
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
          }), `${a.username} 저장했습니다`)}>저장</button>
          <button type="button" className="btn" onClick={() => run(() => api.patchAccount(a.id!, { disabled: !a.disabled }), `${a.username} ${a.disabled ? "사용 재개" : "사용 중지"}`)}>
            {a.disabled ? "사용 재개" : "사용 중지"}
          </button>
          <span className="sp" />
          <button type="button" className="btn danger" onClick={() => confirm(`${a.username} 계정을 지울까요? 되돌릴 수 없습니다.`) && run(() => api.deleteAccount(a.id!), `${a.username} 지움`)}>삭제</button>
        </div>
        <label className="w2">비밀번호 재설정 <small>3종류 8자·2종류 10자 이상 — 지금 로그인은 끊기고, 잠금도 풀리며, 본인이 로그인하면 바꿔야 합니다</small>
          <span className="bar2" style={{ flexWrap: "nowrap" }}>
            <input type="password" value={pw} autoComplete="new-password" style={{ flex: 1 }} onChange={(e) => setPw(e.target.value)} />
            <button type="button" className="btn" disabled={!!passwordProblem(pw)} title={passwordProblem(pw) ?? ""} onClick={() => run(() => api.resetPassword(a.id!, pw), `${a.username} 비밀번호 재설정`)}>재설정</button>
          </span></label>
        {err && <div className="w2 err">{err}</div>}
      </div>
    </div>
  );
}

export default function Accounts({ role }: { role: string | null }) {
  const [data, setData] = useState<AccountList | null>(null);
  const [logs, setLogs] = useState<LoginRecord[]>([]);
  const [logTotal, setLogTotal] = useState(0);
  const [logPage, setLogPage] = useState(1);
  const [logSize, setLogSize] = useState(DEFAULT_PAGE_SIZE);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [edit, setEdit] = useState<number | null>(null);
  const load = () => api.accounts().then((d) => (setData(d), setErr(null))).catch((e) => setErr(errorText(e)));
  const loadLogs = () => api.loginLog({ page: logPage, size: logSize })
    .then((l) => (setLogs(l.items), setLogTotal(l.total))).catch((e) => setErr(errorText(e)));
  useEffect(() => { if (role === "super_admin") load(); }, [role]);
  useEffect(() => { if (role === "super_admin") loadLogs(); }, [role, logPage, logSize]);

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
                  <td>
                    {a.locked_until
                      ? <><span className="c-alarm">잠김 · {new Date(a.locked_until).toTimeString().slice(0, 5)}까지</span>{" "}
                          <button type="button" className="btn sm" onClick={() => api.unlockAccount(a.username).then(() => (setMsg(`${a.username} 잠금 해제`), load())).catch((e) => setErr(errorText(e)))}>잠금 해제</button></>
                      : a.source === "env" ? <span className="muted">기본 관리자 계정</span> : a.disabled ? <span className="c-alarm">사용 중지</span> : a.expired ? <span className="c-alarm">만료</span> : "사용 중"}
                    {(a.must_change_password || a.password_expired) && <div className="muted">{a.must_change_password ? "비밀번호 변경 전" : "비밀번호 90일 지남"}</div>}
                  </td>
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
      <Card title="로그인 기록" className="full" meta={`${nf(logTotal)}건 · 1년 보관`}>
        <div className="bar2">
          <span className="sp" />
          <button type="button" className="btn sm" onClick={loadLogs}>새로고침</button>
          <PageSize size={logSize} onChange={(n) => (setLogSize(n), setLogPage(1))} />
        </div>
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
        <Pager page={logPage} pages={pageCount(logTotal, logSize)} total={logTotal} size={logSize} onPage={setLogPage} />
      </Card>
    </div>
  );
}
