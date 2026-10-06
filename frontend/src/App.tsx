import { useCallback, useEffect, useState } from "react";
import { api, DeviceCounts, Health, Me, Role, errorText } from "./api";
import Dashboard from "./Dashboard";
import DeviceList from "./DeviceList";
import DeviceDetail from "./DeviceDetail";
import Approval from "./Approval";
import Pending from "./Pending";
import DeviceConfig from "./DeviceConfig";
import Profiles from "./Profiles";
import System from "./System";
import Accounts from "./Accounts";
import { PasswordModal } from "./Login";
import ServerSettings from "./ServerSettings";
import Regions from "./Regions";
import GroupControl from "./GroupControl";
import Alarms from "./Alarms";
import Schedule from "./Schedule";
import { logout } from "./Login";
import { nf } from "./ui";

const REFRESH_MS = 10_000;

type Page = "dash" | "alarms" | "devices" | "pending" | "group" | "regions" | "config" | "schedule" | "profiles" | "server" | "accounts" | "system";

/** 역할별로 보이는 메뉴(문제점 21번, ADR-013). 게스트는 대시보드만, 지역관리자는 서버·계정 관련을 뺀다. */
const SUPER_PAGES: Page[] = ["profiles", "server", "accounts", "system"];
export function pageAllowed(id: Page, role: Role | undefined): boolean {
  if (role === "guest") return id === "dash";
  if (role === "super_admin" || role === undefined) return !(role === undefined && SUPER_PAGES.includes(id));
  return !SUPER_PAGES.includes(id);
}

const PAGES: { id: Page; ico: string; label: string; title: string }[] = [
  { id: "dash", ico: "▦", label: "대시보드", title: "통합 관제 대시보드" },
  { id: "alarms", ico: "!", label: "알람", title: "알람 (조치 필요)" },
  { id: "devices", ico: "≡", label: "단말 목록", title: "단말 목록" },
  { id: "pending", ico: "＋", label: "단말 등록·승인", title: "단말 등록·승인" },
  { id: "group", ico: "⊞", label: "그룹 제어", title: "그룹 제어" },
  { id: "regions", ico: "⌥", label: "지역(법정동)", title: "지역(법정동) 트리" },
  { id: "config", ico: "≣", label: "단말 설정", title: "단말 설정" },
  { id: "schedule", ico: "◷", label: "그룹 스케줄 변경", title: "그룹 스케줄 변경" },
  { id: "profiles", ico: "◫", label: "통신 주기 설정", title: "통신 주기 설정" },
  { id: "server", ico: "☰", label: "서버 설정", title: "서버 설정" },
  { id: "accounts", ico: "☺", label: "계정 관리", title: "계정 관리" },
  { id: "system", ico: "⚙", label: "서버 상태", title: "서버 상태" },
];
const ROLE_LABEL: Record<string, string> = { super_admin: "최고관리자", region_admin: "지역관리자", guest: "게스트", admin: "관리자" };
const noop = () => undefined;

function pageFromHash(): Page {
  // "#config/<UUID>" 처럼 뒤에 붙은 인자는 페이지가 직접 읽는다.
  const h = location.hash.replace(/^#/, "").split("/")[0];
  return (PAGES.find((p) => p.id === h)?.id ?? "dash") as Page;
}

function themeNow(): "dark" | "light" {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [health, setHealth] = useState<Health | null>(null);
  const [counts, setCounts] = useState<DeviceCounts | null>(null);
  const [alarmN, setAlarmN] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null); // 드로어에 띄운 단말
  const [tick, setTick] = useState(0);
  const [updAt, setUpdAt] = useState<Date | null>(null);
  const [now, setNow] = useState(new Date());
  const [theme, setTheme] = useState<"dark" | "light">(themeNow);
  const [me, setMe] = useState<Me | null>(null);
  const [meErr, setMeErr] = useState<string | null>(null);
  const [pwOpen, setPwOpen] = useState(false);
  const guest = me?.role === "guest";
  const isSuper = !me || me.role === "super_admin";
  // 역할에 없는 메뉴로 들어오면(주소창에 #… 직접) 대시보드로
  useEffect(() => {
    if (me && !pageAllowed(page, me.role)) location.hash = "#dash";
  }, [me, page]);

  // 사용자·역할(5차 §3.9.3 #9). 실패하면 관리자 권한으로 본다(최고관리자 버튼을 숨긴다).
  useEffect(() => {
    api.me().then((m) => (setMe(m), setMeErr(null))).catch((e) => setMeErr(errorText(e)));
  }, [tick]);

  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  /** 요약: /health 1회 + 목록 API 1회(size=1, counts 만 쓴다). */
  const refresh = useCallback(async () => {
    try {
      const [h, l, a] = await Promise.all([
        api.health(), api.listDevices({ page: 1, size: 1 }),
        api.alarms({ status: "open", size: 1 }).catch(() => null),
      ]);
      setHealth(h);
      setCounts(l.counts);
      setAlarmN(a ? a.counts.all : null);
      setUpdAt(new Date());
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(id);
  }, [refresh, tick]);

  // ESC 로 드로어 닫기
  useEffect(() => {
    if (!selected) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setSelected(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  function toggleTheme() {
    const t = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("slc-theme", t); } catch { /* 무시 */ }
    setTheme(t);
  }

  const pending = counts?.PENDING ?? 0;
  const total = counts ? counts.PENDING + counts.ACTIVE + counts.SUSPENDED + counts.REJECTED + counts.RETIRED : null;
  // 서버 이상이면 무엇이 문제인지 그 자리에 적는다(문제점 41번). 정상이면 "서버 정상" 한 마디.
  const problems: string[] = [];
  if (error && !health) problems.push("서버 응답 없음");
  if (health && !health.mqtt_connected) problems.push("단말 통신 서버 끊김");
  if (health && !health.db_ok) problems.push("DB 장애");
  if (health && !health.broker_log_tail) problems.push("단말 접속 기록 중단");
  const serverOk = !!health && problems.length === 0;
  const cur = PAGES.find((p) => p.id === page)!;

  return (
    <div className="shell">
      <aside className="side">
        <div className="logo"><b>Solar Light Control</b><span>태양광 조명 통합관제</span></div>
        <nav className="nav">
          {PAGES.filter((p) => pageAllowed(p.id, me?.role)).map((p) => (
            <a key={p.id} href={`#${p.id}`} className={page === p.id ? "on" : ""}>
              <span className="ico">{p.ico}</span>{p.label}
              {p.id === "pending" && pending > 0 && <span className="cnt b">{nf(pending)}</span>}
              {p.id === "alarms" && !!alarmN && <span className="cnt">{nf(alarmN)}</span>}
            </a>
          ))}
        </nav>
        {/* 왼쪽 아래 — 약관·저작권(문제점 42번). 서버 상태 줄은 위쪽 "서버 정상" 알약 하나로(41번). */}
        <div className="foot legal">
          <div>이용약관 · 개인정보 처리방침</div>
          <div>© 2026, RMNECO. All rights reserved.</div>
        </div>
      </aside>

      <main>
        <header className="top">
          <div>
            <h1>{cur.title}</h1>
            <div className="sub">
              {updAt ? `마지막 갱신 ${updAt.toLocaleDateString("ko-KR")} ${updAt.toTimeString().slice(0, 8)}` : "불러오는 중…"}
              {error && <span className="c-alarm"> · {error}</span>}
            </div>
          </div>
          <span className="sp" />
          {isSuper && health?.test_account_enabled && (
            <span className="pill warn" title="MQTT_TEST_ACCOUNT_ENABLED — 1차 공용 시험 계정(solarlte-test)이 아직 열려 있음. 운영 전 닫을 것">
              <span className="dot" style={{ background: "var(--warn)" }} />공용 시험 계정 열림
            </span>
          )}
          {health && health.telemetry_dropped > 0 && (
            <span className="pill alarm" title="telemetry_dropped">Telemetry 유실 {nf(health.telemetry_dropped)}</span>
          )}
          <span className={`pill ${problems.length ? "alarm" : ""}`} title="자세한 내용은 서버 상태 화면">
            <span className="dot" style={{ background: serverOk ? "var(--ok)" : problems.length ? "var(--alarm)" : "var(--off)" }} />
            {serverOk ? "서버 정상" : problems.length ? problems.join(" · ") : "서버 확인 중"}
          </span>
{isSuper && (
                    <span className="pill" title="활성 HMAC 키(ADR-003)">
            HMAC {health ? (health.hmac_keys?.length ? health.hmac_keys.join(", ") : "없음") : "-"}
          </span>
          )}
          <span className={`pill ${me?.role === "super_admin" ? "role" : ""}`}
            title={meErr ?? (me?.regions?.length ? `맡은 시·도: ${me.regions.join(", ")}` : "전 지역") + (me?.expires_at ? ` · ${new Date(me.expires_at).toLocaleDateString("ko-KR")} 까지` : "")}>
            {me ? `${me.user} · ${ROLE_LABEL[me.role] ?? me.role}${me.regions?.length ? ` · ${me.regions.join("·")}` : ""}` : meErr ? "사용자 확인 실패" : "사용자 -"}
          </span>
          {me?.can_change_password && <button className="btn" onClick={() => setPwOpen(true)} title="내 비밀번호 바꾸기">비밀번호</button>}
          <button className="btn" onClick={logout} title="로그아웃">로그아웃</button>
          <span className="pill clock">{now.toLocaleDateString("ko-KR")} {now.toTimeString().slice(0, 8)}</span>
          <button className="btn" onClick={() => setTick((t) => t + 1)} title="지금 다시 읽기(자동 10초)">새로고침</button>
          <button className="btn icon" onClick={toggleTheme} aria-label={theme === "dark" ? "밝은 화면으로" : "어두운 화면으로"}>
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </header>

        {page === "dash" && <Dashboard counts={counts} total={total} health={health} tick={tick} onSelect={guest ? noop : setSelected} readOnly={guest} />}
        {page === "devices" && (
          <div className="content">
            <DeviceList tick={tick} selected={selected} onSelect={setSelected} />
          </div>
        )}
        {page === "pending" && <Pending tick={tick} onSelect={setSelected} canList={me?.role === "super_admin" || me?.role === "admin" || !me} />}
        {page === "group" && <GroupControl role={me?.role ?? null} counts={counts} tick={tick} onSelect={setSelected} />}
        {page === "regions" && <Regions role={me?.role ?? null} health={health} tick={tick} onSelect={setSelected} />}
        {page === "config" && <DeviceConfig tick={tick} onSelect={setSelected} />}
        {page === "alarms" && <Alarms tick={tick} onSelect={setSelected} />}
        {page === "schedule" && <Schedule tick={tick} onSelect={setSelected} />}
        {page === "profiles" && (
          <div className="content">
            <Profiles onChanged={refresh} />
          </div>
        )}
        {page === "server" && <ServerSettings role={me?.role ?? null} />}
        {page === "accounts" && <Accounts role={me?.role ?? null} />}
        {page === "system" && <System role={me?.role ?? null} />}
      </main>

      {pwOpen && <PasswordModal onClose={() => setPwOpen(false)} />}
      {selected && <div className="ov" onClick={() => setSelected(null)} />}
      <aside className={`drawer wide ${selected ? "open" : ""}`} aria-label="단말 상세" aria-hidden={!selected}>
        {selected && (
          <DeviceDrawer
            key={selected}
            uuid={selected}
            onChanged={() => (refresh(), setTick((t) => t + 1))}
            onClose={() => setSelected(null)}
            onDeleted={() => {
              setSelected(null);
              refresh();
              setTick((t) => t + 1);
            }}
          />
        )}
      </aside>
    </div>
  );
}

/** 승인 대기(PENDING) 단말은 승인 전용 창, 그 밖은 단말 상세(문제점 #4·#6). 어디서 고르든 같은 규칙. */
function DeviceDrawer({ uuid, onChanged, onClose, onDeleted }: {
  uuid: string; onChanged: () => void; onClose: () => void; onDeleted: () => void;
}) {
  const [state, setState] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const check = useCallback(() => {
    api.getDevice(uuid).then((d) => (setState(d.state), setErr(null))).catch((e) => setErr(errorText(e)));
  }, [uuid]);
  useEffect(check, [check]);
  if (err && state === null) return <div className="db"><div className="err">{err}</div></div>;
  if (state === null) return <div className="db"><div className="muted">불러오는 중…</div></div>;
  if (state === "PENDING")
    return <Approval uuid={uuid} onClose={onClose} onChanged={() => (onChanged(), check())} />;
  return <DeviceDetail uuid={uuid} onChanged={() => (onChanged(), check())} onClose={onClose} onDeleted={onDeleted} />;
}
