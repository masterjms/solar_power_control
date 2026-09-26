import { useEffect, useState } from "react";
import { api, Health, errorText } from "./api";
import { Card, Met, nf } from "./ui";

const REFRESH_MS = 10_000;

/** 시스템: /health 요약 타일 + /health, /api/metrics 원본 JSON. 10초마다 갱신. */
export default function System() {
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Record<string, unknown> | null>(null);
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

  const okv = (b: boolean | undefined) => (b === undefined ? "-" : b ? "정상" : "이상");
  const okc = (b: boolean | undefined) => (b === undefined ? "" : b ? "k" : "a");

  return (
    <div className="content">
      <Card title="서버 상태" meta={`${at || "-"} 갱신 · 10초마다`}>
        {error && <div className="err">{error}</div>}
        <div className="grid2" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
          <Met l="MQTT 연결" v={okv(health?.mqtt_connected)} cls={okc(health?.mqtt_connected)} />
          <Met l="DB" v={okv(health?.db_ok)} cls={okc(health?.db_ok)} />
          <Met l="브로커 로그 추적" v={health ? (health.broker_log_tail ? "추적 중" : "중단") : "-"} cls={okc(health?.broker_log_tail)} h="presence 갱신(ADR-004)" />
          <Met l="HMAC 키" v={health ? (health.hmac_keys?.length ? health.hmac_keys.join(", ") : "없음") : "-"} h="활성 키 이름(ADR-003)" />
          <Met l="버퍼 대기" v={nf(health?.buffer_pending)} h="telemetry flush 전" />
          <Met l="등록 큐" v={nf(health?.register_queue)} />
          <Met l="Telemetry 유실" v={nf(health?.telemetry_dropped)} cls={health?.telemetry_dropped ? "a" : ""} h={`flush 실패 ${nf(health?.flush_failures)}`} />
          <Met l="공용 시험 계정" v={health ? (health.test_account_enabled ? "열림" : "닫힘") : "-"} cls={health?.test_account_enabled ? "w" : "k"} h="운영 전 닫을 것" />
        </div>
        <div className="cap">env {health?.env ?? "-"}</div>
      </Card>
      <Card title="GET /health" meta="원본">
        <pre className="json">{health ? JSON.stringify(health, null, 2) : "-"}</pre>
      </Card>
      <Card title="GET /api/metrics" className="full" meta="프로세스 카운터 (docs/02 §10)">
        <pre className="json">{metrics ? JSON.stringify(metrics, null, 2) : "-"}</pre>
      </Card>
    </div>
  );
}
