// 로그인 화면(문제점 16번) — 브라우저 Basic auth 창 대신. 스마트폰 메신저 안 브라우저에서도 뜬다.
// 계정은 서버 .env 의 ADMIN_* / OPERATOR_*. 세션은 쿠키(기본 7일), 판정은 nginx auth_request → backend.
import { FormEvent, ReactNode, useEffect, useState } from "react";
import { api, ApiErrorException, AUTH_EVENT, errorText } from "./api";

export function Login({ onDone }: { onDone: () => void }) {
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!user.trim() || !pw) return setErr("사용자 이름과 비밀번호를 넣는다");
    setBusy(true);
    setErr(null);
    try {
      await api.login(user.trim(), pw);
      onDone();
    } catch (x) {
      setErr(x instanceof ApiErrorException && x.status === 429 ? "시도가 너무 많습니다 — 1분 뒤 다시" : errorText(x));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="login-box" onSubmit={submit}>
        <div className="logo"><b>Solar Light Control</b><span>태양광 조명 통합관제</span></div>
        <label>사용자 이름
          <input className="inp" value={user} autoComplete="username" autoCapitalize="none" autoCorrect="off" spellCheck={false}
            onChange={(e) => setUser(e.target.value)} autoFocus />
        </label>
        <label>비밀번호
          <input className="inp" type="password" value={pw} autoComplete="current-password" onChange={(e) => setPw(e.target.value)} />
        </label>
        {err && <div className="err">{err}</div>}
        <button type="submit" className="btn pri" disabled={busy}>{busy ? "확인 중…" : "로그인"}</button>
      </form>
    </div>
  );
}

/** 로그인 여부를 보고 화면 또는 로그인 창. 쓰는 중에 세션이 끝나면(401) 로그인 창으로 돌아간다. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"checking" | "in" | "out">("checking");
  useEffect(() => {
    const out = () => setState("out");
    window.addEventListener(AUTH_EVENT, out);
    // 401 이 아닌 실패(서버 문제)는 화면으로 들어가 그 안에서 보인다.
    api.me().then(() => setState("in")).catch((e) => setState(e instanceof ApiErrorException && e.status === 401 ? "out" : "in"));
    return () => window.removeEventListener(AUTH_EVENT, out);
  }, []);
  if (state === "checking") return <div className="login"><div className="muted">확인 중…</div></div>;
  if (state === "out") return <Login onDone={() => setState("in")} />;
  return <>{children}</>;
}

export async function logout() {
  try {
    await api.logout();
  } finally {
    location.reload();
  }
}
