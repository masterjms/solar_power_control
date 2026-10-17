// 로그인 화면(문제점 16번) — 브라우저 Basic auth 창 대신. 스마트폰 메신저 안 브라우저에서도 뜬다.
// 2026-10-14 문제점 25번: 단말측 시안(login.html)대로 — 회사 바탕 이미지·별·빛 입자·유리 상자·떠오르는 칸 이름·로그인 유지.
// 계정은 서버 .env 의 ADMIN_* / OPERATOR_*. 세션은 쿠키(기본 7일), 판정은 nginx auth_request → backend.
import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { api, ApiErrorException, AUTH_EVENT, userMessage } from "./api";

export function Login({ onDone }: { onDone: () => void }) {
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const [remember, setRemember] = useState(true);
  const [showPw, setShowPw] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [hint, setHint] = useState(false);
  const card = useRef<HTMLFormElement | null>(null);
  const [srv, setSrv] = useState<"ok" | "down" | null>(null);
  // 자동 로그아웃으로 돌아왔으면 한 번 알려 준다(문제점 54번)
  const [idleNote] = useState<string | null>(() => {
    try {
      const m = sessionStorage.getItem(IDLE_FLAG);
      sessionStorage.removeItem(IDLE_FLAG);
      return m ? `${m}분 동안 사용하지 않아 자동으로 로그아웃했습니다.` : null;
    } catch { return null; }
  });

  // 서버 상태 한 줄 — 로그인 전에도 열린 /healthz(ok/down)
  useEffect(() => {
    let alive = true;
    const check = () => fetch("/healthz", { cache: "no-store" }).then((r) => alive && setSrv(r.ok ? "ok" : "down")).catch(() => alive && setSrv("down"));
    check();
    const t = setInterval(check, 30_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // 틀리면 상자를 흔든다 — 다시 그리지 않고 애니메이션만 다시 건다(입력 칸 포커스 유지)
  const fail = (m: string) => {
    setErr(m);
    const el = card.current;
    if (!el) return;
    el.classList.remove("lg-shake");
    void el.offsetWidth;
    el.classList.add("lg-shake");
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!user.trim() || !pw) return fail("아이디와 비밀번호를 넣으세요.");
    setBusy(true);
    setErr(null);
    try {
      await api.login(user.trim(), pw, remember);
      onDone();
    } catch (x) {
      fail(x instanceof ApiErrorException && x.status === 429 ? "시도가 너무 많습니다 — 1분 뒤 다시 하세요." : userMessage(x));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="lg-page">
      <LoginBackdrop />
      <main className="lg-main">
        <form ref={card} className="lg-card" onSubmit={submit} noValidate>
          <LoginBrand />
          <div className="lg-field">
            <input id="lg-uid" value={user} placeholder=" " autoComplete="username" autoCapitalize="none" autoCorrect="off" spellCheck={false}
              onChange={(e) => setUser(e.target.value)} autoFocus />
            <label htmlFor="lg-uid">아이디</label>
            <IconUser />
          </div>
          <div className="lg-field">
            <input id="lg-pw" type={showPw ? "text" : "password"} value={pw} placeholder=" " autoComplete="current-password" onChange={(e) => setPw(e.target.value)} />
            <label htmlFor="lg-pw">비밀번호</label>
            <IconLock />
            <button type="button" className="lg-toggle" onClick={() => setShowPw((v) => !v)} aria-label={showPw ? "비밀번호 숨기기" : "비밀번호 보기"}>
              {showPw ? <IconEyeOff /> : <IconEye />}
            </button>
          </div>
          <div className="lg-row">
            <label><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> 로그인 유지</label>
            <button type="button" className="lg-link" onClick={() => setHint((v) => !v)}>비밀번호 찾기</button>
          </div>
          {hint && <div className="lg-hint">비밀번호는 최고관리자가 계정 관리에서 다시 정해 줍니다 — 관리자에게 문의하세요.</div>}
          {idleNote && !err && <div className="lg-hint">{idleNote}</div>}
          {err && <div className="lg-err" role="alert">{err}</div>}
          <button className={`lg-btn ${busy ? "loading" : ""}`} type="submit" disabled={busy}>
            <span className="label">로그인</span><span className="spin" />
          </button>
          <div className="lg-status">
            <span className={`lg-dot ${srv === "down" ? "down" : srv === null ? "wait" : ""}`} />
            <span>{srv === "down" ? "서버에 연결할 수 없습니다 — 잠시 뒤 다시 시도하세요" : srv === null ? "서버 확인 중…" : "시스템 정상 운영 중"}</span>
          </div>
        </form>
      </main>
      <footer className="lg-foot">© 2026, RMNECO. All rights reserved.</footer>
    </div>
  );
}

/** 로그인 바탕 — 회사 이미지(public/brand/login-bg.jpg)가 천천히 확대·마우스를 따라 살짝 움직이고, 위에 별 반짝임·떠오르는 빛.
 *  움직임 줄이기(prefers-reduced-motion)를 켠 사람에겐 멈춘 그림만. */
function LoginBackdrop() {
  const cv = useRef<HTMLCanvasElement | null>(null);
  const wrap = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const c = cv.current;
    const x = c?.getContext("2d");
    if (!c || !x) return;
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    type Star = { x: number; y: number; r: number; p: number; s: number };
    type Mote = { x: number; y: number; r: number; vx: number; vy: number; life: number; max: number; hue: string };
    let W = 0, H = 0, stars: Star[] = [], motes: Mote[] = [], raf = 0;
    const mote = (): Mote => ({
      x: Math.random() * W, y: H * 0.55 + Math.random() * H * 0.5, r: Math.random() * 1.6 + 0.6,
      vy: -(Math.random() * 0.25 + 0.08), vx: (Math.random() - 0.5) * 0.15, life: 0, max: 400 + Math.random() * 500,
      hue: Math.random() < 0.6 ? "255,200,120" : "120,200,255",
    });
    const size = () => {
      W = innerWidth; H = innerHeight;
      c.width = W * dpr; c.height = H * dpr; c.style.width = `${W}px`; c.style.height = `${H}px`;
      x.setTransform(dpr, 0, 0, dpr, 0, 0);
      stars = Array.from({ length: Math.round((W * H) / 9000) }, () => ({ x: Math.random() * W, y: Math.random() * H * 0.45, r: Math.random() * 1.1 + 0.3, p: Math.random() * Math.PI * 2, s: Math.random() * 1.5 + 0.5 }));
      motes = Array.from({ length: Math.round(W / 45) }, mote);
    };
    const frame = (t: number) => {
      x.clearRect(0, 0, W, H);
      for (const s of stars) {
        const a = 0.25 + 0.75 * Math.abs(Math.sin((t / 1000) * s.s + s.p));
        x.fillStyle = `rgba(255,255,255,${a * 0.8})`;
        x.beginPath(); x.arc(s.x, s.y, s.r, 0, 7); x.fill();
      }
      for (let i = 0; i < motes.length; i++) {
        const m = motes[i];
        m.x += m.vx; m.y += m.vy; m.life++;
        const k = Math.sin((Math.PI * m.life) / m.max);
        const g = x.createRadialGradient(m.x, m.y, 0, m.x, m.y, m.r * 6);
        g.addColorStop(0, `rgba(${m.hue},${0.7 * k})`); g.addColorStop(1, `rgba(${m.hue},0)`);
        x.fillStyle = g; x.beginPath(); x.arc(m.x, m.y, m.r * 6, 0, 7); x.fill();
        if (m.life >= m.max) motes[i] = mote();
      }
      if (!reduce) raf = requestAnimationFrame(frame);
    };
    const move = (e: PointerEvent) => {
      if (wrap.current) wrap.current.style.transform = `translate(${(e.clientX / W - 0.5) * -14}px, ${(e.clientY / H - 0.5) * -10}px)`;
    };
    size();
    raf = requestAnimationFrame(frame);
    addEventListener("resize", size);
    if (!reduce) addEventListener("pointermove", move);
    return () => {
      cancelAnimationFrame(raf);
      removeEventListener("resize", size);
      removeEventListener("pointermove", move);
    };
  }, []);
  return (
    <>
      <div className="lg-bgwrap" ref={wrap}><div className="lg-bg" /></div>
      <div className="lg-shade" />
      <canvas className="lg-fx" ref={cv} aria-hidden="true" />
    </>
  );
}

/** 로그인 상자 위 — 회사 로고 이미지(public/brand/logo-login.*)가 있으면 그것, 없으면 해 아이콘 + 이름. */
function LoginBrand() {
  const srcs = ["/brand/logo-login.png", "/brand/logo-login.svg"];
  const [i, setI] = useState(0);
  if (i < srcs.length) return <div className="lg-brand img"><img src={srcs[i]} alt="태양광 통합 관제 시스템" onError={() => setI((n) => n + 1)} /></div>;
  return (
    <div className="lg-brand">
      <div className="lg-logo">
        <svg viewBox="0 0 24 24" fill="none" stroke="#06121f" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
      </div>
      <div>
        <h1>태양광 통합 관제 시스템</h1>
        <p>SOLAR MONITORING CENTER</p>
      </div>
    </div>
  );
}

/** 회사 로고(문제점 25번) — public/brand/ 의 이미지, 없으면 글자 로고. variant: light·dark(왼쪽 메뉴 위, 테마별). */
export function BrandLogo({ variant }: { variant: "light" | "dark" }) {
  const srcs = [`/brand/logo-${variant}.png`, `/brand/logo-${variant}.svg`];
  const [i, setI] = useState(0);
  useEffect(() => setI(0), [variant]);
  if (i >= srcs.length) return <div className="logo"><b>Solar Light Control</b><span>태양광 조명 통합관제</span></div>;
  return <div className="logo img"><img src={srcs[i]} alt="태양광 조명 통합관제" onError={() => setI((n) => n + 1)} /></div>;
}

const svg = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };
const IconUser = () => <svg {...svg}><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7" /></svg>;
const IconLock = () => <svg {...svg}><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></svg>;
const IconEye = () => <svg {...svg}><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></svg>;
const IconEyeOff = () => <svg {...svg}><path d="M3 3l18 18" /><path d="M10.6 5.1A10.8 10.8 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.2M6.6 6.6C3.9 8.4 2 12 2 12s3.6 7 10 7c1.7 0 3.2-.4 4.5-1.1" /><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" /></svg>;

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
  if (state === "checking") return <div className="lg-page"><div className="lg-shade" /><main className="lg-main"><div className="lg-wait">확인 중…</div></main></div>;
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

/** 내 비밀번호 바꾸기(화면에서 만든 계정만, 문제점 21번). 바꾸면 다시 로그인한다. */
/** 비밀번호 규칙(서버와 같은 것, 문제점 54번) — 3종 8자 이상 또는 2종 10자 이상. 화면에서 먼저 알려 주기만 하고 판정은 서버. */
export function passwordProblem(pw: string): string | null {
  const kinds = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((r) => r.test(pw)).length;
  return (pw.length >= 8 && kinds >= 3) || (pw.length >= 10 && kinds >= 2) ? null : PASSWORD_RULE;
}
export const PASSWORD_RULE = "영문 대문자·소문자·숫자·특수문자 중 3종류 이상 8자 이상, 또는 2종류 이상 10자 이상";

/** 내 비밀번호 바꾸기(화면에서 만든 계정만, 문제점 21번). 바꾸면 다시 로그인한다.
 *  forced = 바꿔야만 쓸 수 있을 때(관리자가 정한 비밀번호·90일 지남, 문제점 54번) — 닫기 없음, 로그아웃만. */
export function PasswordModal({ onClose, forced = null }: { onClose: () => void; forced?: string | null }) {
  const [old, setOld] = useState("");
  const [nw, setNw] = useState("");
  const [nw2, setNw2] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    const why = passwordProblem(nw);
    if (why) return setErr(`새 비밀번호: ${why}`);
    if (nw !== nw2) return setErr("새 비밀번호 두 칸이 다릅니다");
    if (nw === old) return setErr("지금 비밀번호와 다른 것으로 정하세요");
    setBusy(true);
    setErr(null);
    try {
      await api.changeMyPassword(old, nw);
      alert("비밀번호를 바꿨습니다. 새 비밀번호로 다시 로그인하세요.");
      location.reload();
    } catch (x) {
      setErr(userMessage(x));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="modal-ov" onClick={(e) => !forced && e.target === e.currentTarget && onClose()}>
      <form className="modal" onSubmit={submit} role="dialog" aria-label="비밀번호 바꾸기">
        <div className="dh"><h3>비밀번호 바꾸기</h3>
          {!forced && <button type="button" className="x" onClick={onClose} aria-label="닫기">✕</button>}</div>
        <div className="db form2">
          {forced && <div className="w2 cap c-warn">{forced}</div>}
          <label className="w2">지금 비밀번호<input type="password" value={old} autoComplete="current-password" onChange={(e) => setOld(e.target.value)} /></label>
          <label>새 비밀번호<input type="password" value={nw} autoComplete="new-password" onChange={(e) => setNw(e.target.value)} /></label>
          <label>새 비밀번호 한 번 더<input type="password" value={nw2} autoComplete="new-password" onChange={(e) => setNw2(e.target.value)} /></label>
          <div className="w2 cap">{PASSWORD_RULE}. 90일마다 바꿉니다.</div>
          {err && <div className="w2 err">{err}</div>}
          <div className="w2 bar2">
            {forced && <button type="button" className="btn" onClick={logout}>로그아웃</button>}
            <span className="sp" />
            <button type="submit" className="btn pri" disabled={busy || !old || !nw}>{busy ? "바꾸는 중…" : "바꾸기"}</button>
          </div>
        </div>
      </form>
    </div>
  );
}

/** 무동작 자동 로그아웃(문제점 54번 — 공공 기본 항목). 마우스·키보드·터치·스크롤이 minutes 분 동안 없으면 로그아웃.
 *  10초 자동 갱신은 '사용'이 아니다. 탭이 여러 개면 마지막 사용 시각을 localStorage 로 나눠 가진다. 0 이면 끔. */
export const IDLE_KEY = "slc-last-active";
export const IDLE_FLAG = "slc-idle-logout";
export function useIdleLogout(minutes: number | undefined) {
  useEffect(() => {
    if (!minutes) return;
    const mark = () => {
      try { localStorage.setItem(IDLE_KEY, String(Date.now())); } catch { /* 무시 */ }
    };
    let last = Date.now();
    const touch = () => {
      const now = Date.now();
      if (now - last > 5000) { last = now; mark(); } // 5초에 한 번만 적는다
    };
    mark();
    const evs = ["mousemove", "mousedown", "keydown", "touchstart", "wheel", "scroll"] as const;
    evs.forEach((e) => window.addEventListener(e, touch, { passive: true }));
    const t = setInterval(() => {
      let at = last;
      try { at = Math.max(at, Number(localStorage.getItem(IDLE_KEY) || 0)); } catch { /* 무시 */ }
      if (Date.now() - at > minutes * 60_000) {
        try { sessionStorage.setItem(IDLE_FLAG, String(minutes)); } catch { /* 무시 */ }
        void logout();
      }
    }, 20_000);
    return () => {
      evs.forEach((e) => window.removeEventListener(e, touch));
      clearInterval(t);
    };
  }, [minutes]);
}
