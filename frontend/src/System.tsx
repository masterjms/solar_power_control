// 서버 상태(#system) — 최고관리자만(문제점 31번). 단말이 아니라 서버 자체가 괜찮은지 —
// 장애 때 "단말 문제인지 서버 문제인지" 가르는 화면. 다섯 묶음: 브로커 / DB·디스크 / 처리 / 보안 / 서버.
// 원문(/health·/api/metrics)은 "자세히"로 접는다.
import { useEffect, useState } from "react";
import { api, Health, SystemStatus, errorText } from "./api";
import { localTime, relTime } from "./format";
import { Card, Detail, Met, nf } from "./ui";

const REFRESH_MS = 10_000;

function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "-";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} KB`;
  return `${n} B`;
}

function uptime(sec: number): string {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}일 ${h}시간` : h ? `${h}시간 ${m}분` : `${m}분`;
}

const TABLE_LABEL: Record<string, string> = {
  telemetry: "10분 보고 원문", telemetry_daily: "하루 요약", device_event: "단말 이벤트", alarm: "알람",
  command: "원격 명령", command_target: "명령 대상", device: "단말", device_settings_history: "설정 이력",
  deploy_item: "보낸 기록(단말별)", login_log: "로그인 기록", region: "지역 트리"
};

export default function System({ role }: { role: string | null }) {
  const [s, setS] = useState<SystemStatus | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (role !== "super_admin") return;
    let alive = true;
    const load = async () => {
      try {
        const [st, h, m] = await Promise.all([api.systemStatus(), api.health(), api.metrics()]);
        if (!alive) return;
        setS(st); setHealth(h); setMetrics(m); setError(null);
      } catch (e) {
        if (alive) setError(errorText(e));
      }
    };
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => { alive = false; clearInterval(id); };
  }, [role]);

  if (role !== "super_admin")
    return <div className="content"><Card title="서버 상태" className="full"><div className="muted">최고관리자만 볼 수 있습니다.</div></Card></div>;

  const ok = (v: boolean | undefined, yes = "정상", no = "이상") => (v === undefined ? "-" : v ? yes : no);
  const cls = (v: boolean | undefined) => (v === undefined ? "" : v ? "k" : "a");
  const b = s?.broker, d = s?.db, p = s?.processing, sec = s?.security, sv = s?.server;

  return (
    <div className="content">
      {error && <Card title="서버 상태" className="full"><div className="err">{error}</div></Card>}
      <Card title="단말 통신 서버" className="full" meta={s ? `${localTime(s.at)} · 10초마다` : ""}>
        <div className="grid4">
          <Met l="서버 ↔ 단말 통신 서버" v={ok(b?.backend_connected, "연결됨", "끊김")} cls={cls(b?.backend_connected)} h="끊기면 단말 보고·명령이 모두 멈춥니다" />
          <Met l="일반 접속 (1883)" v={ok(b?.port_1883, "듣는 중", "안 들음")} cls={cls(b?.port_1883)} />
          <Met l="암호화 접속 (8883)" v={ok(b?.port_8883, "듣는 중", "안 들음")} cls={b?.port_8883 ? "k" : "w"} h="인증서가 없으면 꺼져 있는 게 정상입니다" />
          <Met l="온라인 단말" v={b ? `${nf(b.devices_online)} / ${nf(b.devices_active)}대` : "-"} h={b ? `접속 중 ${nf(b.devices_broker_connected)}대 · 운영 단말 대비` : ""} />
        </div>
      </Card>
      <Card title="DB · 디스크" className="full">
        <div className="grid4">
          <Met l="데이터베이스" v={ok(d?.ok)} cls={cls(d?.ok)} h={d ? `크기 ${bytes(d.size_bytes)}` : ""} />
          <Met l="디스크 사용" v={d?.disk_used_pct !== null && d?.disk_used_pct !== undefined ? `${d.disk_used_pct}%` : "-"} cls={d?.disk_warn ? "a" : "k"}
            h={d ? `여유 ${bytes(d.disk_free_bytes)} / ${bytes(d.disk_total_bytes)} · 80% 넘으면 경고` : ""} />
          <Met l="마지막 하루 집계" v={d?.last_rollup_at ? relTime(d.last_rollup_at) : "재시작 뒤 아직"} h={d?.last_rollup_day ? `${d.last_rollup_day} 분 · 매일 00:30` : "매일 00:30"} />
          <Met l="마지막 보관 정리" v={d?.last_purge_at ? relTime(d.last_purge_at) : "-"} h={d ? `10분 보고 원문 ${d.telemetry_months}개월 보관 · 매일 01:00` : ""} />
        </div>
        {d && d.tables.length > 0 && (
          <div className="tw" style={{ marginTop: 8 }}>
            <table className="mini">
              <thead><tr><th>큰 표</th><th className="n">크기</th></tr></thead>
              <tbody>{d.tables.map((t) => <tr key={t.name}><td>{TABLE_LABEL[t.name] ?? t.name}</td><td className="n">{bytes(t.bytes)}</td></tr>)}</tbody>
            </table>
          </div>
        )}
      </Card>
      <Card title="처리" className="full">
        <div className="grid4">
          <Met l="수신 대기" v={nf(p?.buffer_pending)} h="저장 전 보고 · 평소 0 근처" />
          <Met l="보고 유실" v={nf(p?.telemetry_dropped)} cls={p?.telemetry_dropped ? "a" : "k"} h={`쓰기 실패 ${nf(p?.flush_failures)} · 0 이어야 정상`} />
          <Met l="진행 중 명령" v={nf(p?.commands_open)} h={`등록 응답 대기 ${nf(p?.register_queue)}`} />
          <Met l="스케줄 보내기 진행" v={nf(p?.deploy_open)} h={`단말 통신 서버 재접속 ${nf(p?.mqtt_reconnects)}회(재시작 뒤)`} />
        </div>
      </Card>
      <Card title="보안" className="full">
        <div className="grid4">
          <Met l="단말 인증 키" v={sec?.hmac_keys?.length ? sec.hmac_keys.join(", ") : "없음"} cls={sec?.hmac_keys?.length ? "k" : "a"} h="활성 키 이름(값은 안 보임)" />
          <Met l="공용 시험 계정" v={sec ? (sec.test_account_enabled ? "열림" : "닫힘") : "-"} cls={sec?.test_account_enabled ? "w" : "k"} h="운영 전에 닫아야 합니다" />
          <Met l="로그인 실패 (24시간)" v={nf(sec?.login_failures_24h)} cls={(sec?.login_failures_24h ?? 0) >= 10 ? "w" : ""} h="계정 관리 > 로그인 기록" />
          <Met l="7일 안 만료 계정" v={nf(sec?.accounts_expiring_7d)} cls={sec?.accounts_expiring_7d ? "w" : ""} h="계정 관리에서 연장" />
        </div>
      </Card>
      <Card title="서버" className="full">
        <div className="grid4">
          <Met l="버전" v={<span className="mono">{sv?.version ?? "-"}</span>} h="배포한 커밋" />
          <Met l="가동 시간" v={sv ? uptime(sv.uptime_sec) : "-"} h={sv ? `시작 ${localTime(sv.started_at)}` : ""} />
          <Met l="단말 접속 기록" v={ok(b?.log_tail, "기록 중", "중단")} cls={cls(b?.log_tail)} h="단말 접속·끊김 판정에 씁니다" />
        </div>
        <Detail summary="자세히 — 원문">
          <div className="cap">환경 {sv?.env ?? "-"}</div>
          <pre className="json">{health ? JSON.stringify(health, null, 2) : "-"}</pre>
          <pre className="json">{metrics ? JSON.stringify(metrics, null, 2) : "-"}</pre>
        </Detail>
      </Card>
    </div>
  );
}
