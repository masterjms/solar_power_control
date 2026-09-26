import { useEffect, useState } from "react";
import { api, errorText } from "./api";

const REFRESH_MS = 10_000;

/** 시스템 탭: /health 와 /api/metrics 원본 JSON. 10초마다 갱신. */
export default function System() {
  const [health, setHealth] = useState<unknown>(null);
  const [metrics, setMetrics] = useState<unknown>(null);
  const [at, setAt] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [h, m] = await Promise.all([api.health(), api.metrics()]);
        if (!alive) return;
        setHealth(h);
        setMetrics(m);
        setAt(new Date().toLocaleTimeString("ko-KR", { hour12: false }));
        setError(null);
      } catch (e) {
        if (alive) setError(errorText(e));
      }
    };
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  return (
    <>
      <h3 style={{ margin: "4px 0" }}>시스템 <small>({at || "-"} 갱신, 10초마다)</small></h3>
      <p>
        <small>
          broker_log_tail = 브로커 로그로 presence 갱신 중(ADR-004). hmac_keys = 활성 HMAC 키 이름(ADR-003).
          test_account_enabled = true 면 1차 공용 시험 계정이 아직 열려 있음(운영 전 닫을 것).
        </small>
      </p>
      {error && <div className="error">{error}</div>}
      <fieldset>
        <legend>GET /health</legend>
        <pre className="mono">{health ? JSON.stringify(health, null, 2) : "-"}</pre>
      </fieldset>
      <fieldset>
        <legend>GET /api/metrics</legend>
        <pre className="mono">{metrics ? JSON.stringify(metrics, null, 2) : "-"}</pre>
      </fieldset>
    </>
  );
}
