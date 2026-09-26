import { useCallback, useEffect, useState } from "react";
import { api, DeviceCounts, Health, errorText } from "./api";
import DeviceList from "./DeviceList";
import DeviceDetail from "./DeviceDetail";
import Profiles from "./Profiles";
import System from "./System";

const REFRESH_MS = 10_000;

type Tab = "devices" | "profiles" | "system";

function tabFromHash(): Tab {
  if (location.hash === "#profiles") return "profiles";
  if (location.hash === "#system") return "system";
  return "devices";
}

export default function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const [health, setHealth] = useState<Health | null>(null);
  const [counts, setCounts] = useState<DeviceCounts | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  /** 요약: /health 1회 + 목록 API 1회(size=1, counts 만 쓴다). */
  const refresh = useCallback(async () => {
    try {
      const [h, l] = await Promise.all([api.health(), api.listDevices({ page: 1, size: 1 })]);
      setHealth(h);
      setCounts(l.counts);
      setTotal(l.total);
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

  const pending = counts?.PENDING ?? 0;

  return (
    <>
      <header>
        <nav>
          <a href="#devices" className={tab === "devices" ? "cur" : ""}>단말</a>
          <a href="#profiles" className={tab === "profiles" ? "cur" : ""}>프로필</a>
          <a href="#system" className={tab === "system" ? "cur" : ""}>시스템</a>
        </nav>
        <span className={pending > 0 ? "pending-badge" : ""}>
          승인 대기 <b>{pending}</b>
        </span>
        <span>
          전체 <b>{total}</b> / 온라인 <b className="on">{counts?.online ?? "-"}</b> / 오프라인{" "}
          <b className="off">{counts ? total - counts.online : "-"}</b>
        </span>
        <span>
          {counts
            ? (["ACTIVE", "SUSPENDED", "REJECTED", "RETIRED"] as const).map((s) => `${s} ${counts[s]}`).join(" · ")
            : "-"}
        </span>
        {health ? (
          <span>
            mqtt <b className={health.mqtt_connected ? "on" : "warn"}>{String(health.mqtt_connected)}</b>{" "}
            db <b className={health.db_ok ? "on" : "warn"}>{String(health.db_ok)}</b>{" "}
            broker_log <b className={health.broker_log_tail ? "on" : "warn"}>{String(health.broker_log_tail)}</b>{" "}
            hmac <b>{health.hmac_keys?.length ? health.hmac_keys.join(",") : "없음"}</b>{" "}
            dropped <b className={health.telemetry_dropped ? "warn" : ""}>{health.telemetry_dropped}</b>{" "}
            {health.test_account_enabled && (
              <b className="warn" title="MQTT_TEST_ACCOUNT_ENABLED — 1차 공용 시험 계정(solarlte-test)이 아직 열려 있음">
                공용 시험계정 열림
              </b>
            )}{" "}
            <small>({health.env})</small>
          </span>
        ) : (
          <span>health -</span>
        )}
        <button onClick={() => setTick((t) => t + 1)}>새로고침</button>
        {error && <span className="error">{error}</span>}
      </header>

      {tab === "profiles" ? (
        <div className="right">
          <Profiles onChanged={refresh} />
        </div>
      ) : tab === "system" ? (
        <div className="right">
          <System />
        </div>
      ) : (
        <main>
          <section className="left">
            <DeviceList tick={tick} selected={selected} onSelect={setSelected} />
          </section>
          <section className="right">
            {selected ? (
              <DeviceDetail
                uuid={selected}
                onChanged={refresh}
                onDeleted={() => {
                  setSelected(null);
                  refresh();
                }}
              />
            ) : (
              <p>왼쪽 목록에서 단말을 선택하세요. 승인 대기(PENDING) 단말이 맨 위에 옵니다.</p>
            )}
          </section>
        </main>
      )}
    </>
  );
}
