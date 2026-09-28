import { useCallback, useEffect, useState } from "react";
import { api, DeviceCounts, Health, Me, errorText } from "./api";
import Dashboard from "./Dashboard";
import DeviceList from "./DeviceList";
import DeviceDetail from "./DeviceDetail";
import Approval from "./Approval";
import Pending from "./Pending";
import DeviceConfig from "./DeviceConfig";
import Profiles from "./Profiles";
import System from "./System";
import Regions from "./Regions";
import GroupControl from "./GroupControl";
import { nf } from "./ui";

const REFRESH_MS = 10_000;

type Page = "dash" | "devices" | "pending" | "group" | "regions" | "config" | "profiles" | "system";

const PAGES: { id: Page; ico: string; label: string; title: string }[] = [
  { id: "dash", ico: "▦", label: "대시보드", title: "통합 관제 대시보드" },
  { id: "devices", ico: "≡", label: "단말 목록", title: "단말 목록" },
  { id: "pending", ico: "＋", label: "단말 등록·승인", title: "단말 등록·승인" },
  { id: "group", ico: "⊞", label: "그룹 제어", title: "그룹 제어" },
  { id: "regions", ico: "⌥", label: "지역(법정동)", title: "지역(법정동) 트리" },
  { id: "config", ico: "≣", label: "단말 설정", title: "단말 설정" },
  { id: "profiles", ico: "◫", label: "프로필(설정)", title: "설정 프로필" },
  { id: "system", ico: "⚙", label: "시스템", title: "시스템" },
];
/** 아직 백엔드가 없는 메뉴 — 회색으로만 보인다(docs/00 §2 단계). */
const LATER: { ico: string; label: string; stage: string }[] = [
  { ico: "◎", label: "지도", stage: "7차" },
  { ico: "◷", label: "스케줄", stage: "6차" },
  { ico: "!", label: "알람", stage: "6차" },
  { ico: "⇪", label: "OTA", stage: "7차" },
  { ico: "∿", label: "통계", stage: "7차" },
];

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
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null); // 드로어에 띄운 단말
  const [tick, setTick] = useState(0);
  const [updAt, setUpdAt] = useState<Date | null>(null);
  const [now, setNow] = useState(new Date());
  const [theme, setTheme] = useState<"dark" | "light">(themeNow);
  const [me, setMe] = useState<Me | null>(null);
  const [meErr, setMeErr] = useState<string | null>(null);

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
      const [h, l] = await Promise.all([api.health(), api.listDevices({ page: 1, size: 1 })]);
      setHealth(h);
      setCounts(l.counts);
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
  const serverOk = !!health && health.ok && health.mqtt_connected && health.db_ok;
  const cur = PAGES.find((p) => p.id === page)!;

  const dotOf = (ok: boolean | undefined) =>
    ok === undefined ? "var(--off)" : ok ? "var(--ok)" : "var(--alarm)";

  return (
    <div className="shell">
      <aside className="side">
        <div className="logo"><b>Solar Light Control</b><span>태양광 조명 통합관제</span></div>
        <nav className="nav">
          {PAGES.map((p) => (
            <a key={p.id} href={`#${p.id}`} className={page === p.id ? "on" : ""}>
              <span className="ico">{p.ico}</span>{p.label}
              {p.id === "pending" && pending > 0 && <span className="cnt b">{nf(pending)}</span>}
            </a>
          ))}
          <div className="sep">이후 단계</div>
          {LATER.map((p) => (
            <a key={p.label} className="dis" title={`${p.stage}에 추가`} onClick={(e) => e.preventDefault()} href="#">
              <span className="ico">{p.ico}</span>{p.label}<span className="stg">{p.stage}</span>
            </a>
          ))}
        </nav>
        <div className="foot">
          <div><span className="dot" style={{ background: dotOf(health?.mqtt_connected) }} />MQTT Broker {health ? (health.mqtt_connected ? "정상" : "끊김") : "-"}</div>
          <div><span className="dot" style={{ background: dotOf(health?.db_ok) }} />DB {health ? (health.db_ok ? "정상" : "장애") : "-"}</div>
          <div><span className="dot" style={{ background: dotOf(health?.broker_log_tail) }} />브로커 로그 {health ? (health.broker_log_tail ? "추적 중" : "중단") : "-"}</div>
          <div className="muted">{health ? `env ${health.env}` : ""}</div>
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
          {health?.test_account_enabled && (
            <span className="pill warn" title="MQTT_TEST_ACCOUNT_ENABLED — 1차 공용 시험 계정(solarlte-test)이 아직 열려 있음. 운영 전 닫을 것">
              <span className="dot" style={{ background: "var(--warn)" }} />공용 시험 계정 열림
            </span>
          )}
          {health && health.telemetry_dropped > 0 && (
            <span className="pill alarm" title="telemetry_dropped">Telemetry 유실 {nf(health.telemetry_dropped)}</span>
          )}
          <span className="pill" title={health ? `mqtt ${health.mqtt_connected} · db ${health.db_ok} · broker_log_tail ${health.broker_log_tail}` : ""}>
            <span className="dot" style={{ background: health ? (serverOk ? "var(--ok)" : "var(--alarm)") : "var(--off)" }} />
            {health ? (serverOk ? "서버 정상" : "서버 이상") : "서버 -"}
          </span>
          <span className="pill" title="활성 HMAC 키(ADR-003)">
            HMAC {health ? (health.hmac_keys?.length ? health.hmac_keys.join(", ") : "없음") : "-"}
          </span>
          <span className={`pill ${me?.role === "super_admin" ? "role" : ""}`} title={meErr ?? "nginx Basic auth 사용자 → X-Remote-User (ADR-005)"}>
            {me ? `${me.user} · ${me.role === "super_admin" ? "최고관리자" : "관리자"}` : meErr ? "사용자 확인 실패 · 관리자로 표시" : "사용자 -"}
          </span>
          <span className="pill clock">{now.toLocaleDateString("ko-KR")} {now.toTimeString().slice(0, 8)}</span>
          <button className="btn" onClick={() => setTick((t) => t + 1)} title="지금 다시 읽기(자동 10초)">새로고침</button>
          <button className="btn icon" onClick={toggleTheme} aria-label={theme === "dark" ? "밝은 화면으로" : "어두운 화면으로"}>
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </header>

        {page === "dash" && <Dashboard counts={counts} total={total} health={health} tick={tick} onSelect={setSelected} />}
        {page === "devices" && (
          <div className="content">
            <DeviceList tick={tick} selected={selected} onSelect={setSelected} />
          </div>
        )}
        {page === "pending" && <Pending tick={tick} onSelect={setSelected} />}
        {page === "group" && <GroupControl role={me?.role ?? null} counts={counts} tick={tick} onSelect={setSelected} />}
        {page === "regions" && <Regions role={me?.role ?? null} health={health} tick={tick} onSelect={setSelected} />}
        {page === "config" && <DeviceConfig tick={tick} onSelect={setSelected} />}
        {page === "profiles" && (
          <div className="content">
            <Profiles onChanged={refresh} />
          </div>
        )}
        {page === "system" && <System />}
      </main>

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
