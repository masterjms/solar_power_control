import { useCallback, useEffect, useState } from "react";
import { api, Device, Health, errorText } from "./api";
import DeviceList from "./DeviceList";
import DeviceDetail from "./DeviceDetail";
import ImportAccounts from "./ImportAccounts";

const REFRESH_MS = 10_000;

type Tab = "devices" | "admin";

function tabFromHash(): Tab {
  return location.hash === "#admin" ? "admin" : "devices";
}

/** 전체 단말을 size=500 으로 끝까지 읽어 집계한다(개발용, 규모 커지면 서버 집계 API 로). */
async function fetchAllDevices(): Promise<Device[]> {
  const size = 500;
  const first = await api.listDevices({ page: 1, size });
  const all = [...first.items];
  const pages = Math.ceil(first.total / size);
  for (let p = 2; p <= pages; p++) {
    const r = await api.listDevices({ page: p, size });
    all.push(...r.items);
  }
  return all;
}

export default function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const [health, setHealth] = useState<Health | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [h, d] = await Promise.all([api.health(), fetchAllDevices()]);
      setHealth(h);
      setDevices(d);
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

  const online = devices.filter((d) => d.is_online).length;
  const byState: Record<string, number> = {};
  for (const d of devices) byState[d.state] = (byState[d.state] ?? 0) + 1;

  return (
    <>
      <header>
        <nav>
          <a href="#devices">장치</a>
          <a href="#admin">관리</a>
        </nav>
        <span>
          전체 <b>{devices.length}</b> / 온라인 <b className="on">{online}</b> / 오프라인{" "}
          <b className="off">{devices.length - online}</b>
        </span>
        <span>
          {Object.entries(byState)
            .sort()
            .map(([s, n]) => `${s} ${n}`)
            .join(" · ") || "-"}
        </span>
        {health ? (
          <span>
            mqtt <b className={health.mqtt_connected ? "on" : "warn"}>{String(health.mqtt_connected)}</b>{" "}
            db <b className={health.db_ok ? "on" : "warn"}>{String(health.db_ok)}</b>{" "}
            dropped <b className={health.telemetry_dropped ? "warn" : ""}>{health.telemetry_dropped}</b>{" "}
            <small>({health.env})</small>
          </span>
        ) : (
          <span>health -</span>
        )}
        <button onClick={() => setTick((t) => t + 1)}>새로고침</button>
        {error && <span className="error">{error}</span>}
      </header>

      {tab === "admin" ? (
        <div className="right">
          <ImportAccounts onDone={refresh} />
        </div>
      ) : (
        <main>
          <section className="left">
            <DeviceList devices={devices} selected={selected} onSelect={setSelected} />
          </section>
          <section className="right">
            {selected ? (
              <DeviceDetail
                uuid={selected}
                onDeleted={() => {
                  setSelected(null);
                  refresh();
                }}
              />
            ) : (
              <p>왼쪽 목록에서 단말을 선택하세요.</p>
            )}
          </section>
        </main>
      )}
    </>
  );
}
