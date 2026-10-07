// 알람(조치 필요) 화면 — S-24, 사양서 §16.6, ADR-009. 판정은 서버(30초 재조정), 화면은 목록·이력만.
import { useEffect, useState } from "react";
import { Alarm, AlarmPage, AlarmSeverity, AlarmTab, api, errorText } from "./api";
import { localTime, relTime } from "./format";
import { Card, nf, DEFAULT_PAGE_SIZE, PageSize, Pager } from "./ui";

const REFRESH_MS = 15_000;

export const TAB_LABEL: Record<AlarmTab | "all", string> = {
  all: "전체", fault: "고장", comm: "통신 두절", pending: "승인 대기", config: "설정 불일치", local: "현장 조작",
};
const TABS: (AlarmTab | "all")[] = ["all", "fault", "comm", "pending", "config", "local"];

const SEV: Record<AlarmSeverity, [string, string]> = {
  warn: ["경고", "b-alarm"],
  caution: ["주의", "b-warn"],
  info: ["정보", "b-blue"],
};

export function SeverityBadge({ s }: { s: AlarmSeverity }) {
  const [label, cls] = SEV[s] ?? [s, "b-off"];
  return <span className={`badge ${cls}`}>{label}</span>;
}

export function durText(sec: number): string {
  if (sec < 60) return `${sec}초`;
  if (sec < 3600) return `${Math.floor(sec / 60)}분`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}시간 ${Math.floor((sec % 3600) / 60)}분`;
  return `${Math.floor(sec / 86400)}일 ${Math.floor((sec % 86400) / 3600)}시간`;
}

/** er 비트 → 운영자용 짧은 말. */
const ER_KO: [number, string][] = [
  [0x0001, "배터리 부족"],
  [0x0002, "시계 오류"],
  [0x0004, "LED 고장"],
  [0x0010, "MPPT 응답 없음"],
  [0x0020, "스케줄 오류"],
  [0x0040, "LTE 끊김"],
];

/** 마지막 값 한 줄 — 운영자가 읽는 짧은 결과. 원래 코드·숫자는 alarmRaw(마우스를 올리면). */
export function alarmValue(a: Alarm): string {
  const v = a.value ?? {};
  if (typeof v.er === "number") {
    const er = v.er;
    const names = ER_KO.filter(([bit]) => er & bit).map(([, n]) => n);
    return names.length ? names.join(", ") : er === 0 ? "정상" : "알 수 없는 오류";
  }
  if ("cv_device" in v) return "설정 반영 대기";
  if ("count_today" in v) return `오늘 재부팅 ${v.count_today}회`;
  if ("last_seen_at" in v) return `마지막 수신 ${relTime(v.last_seen_at as string | null)}`;
  if ("ss_known" in v) return "현장에서 설정이 바뀜";
  if ("applied_crc" in v) return "스케줄 다름";
  if ("md" in v) return v.md === 1 ? "현장 수동 조작 중" : v.md === 2 ? "원격 조작 중" : "조작 상태 확인 필요";
  return "";
}

/** 마지막 값의 원래 코드 — 단말측과 맞춰 볼 때(툴팁). */
export function alarmRaw(a: Alarm): string {
  const v = a.value ?? {};
  if (typeof v.er === "number") return `er 0x${v.er.toString(16).padStart(4, "0")}`;
  if ("cv_device" in v) return `cv 단말 ${v.cv_device} / 서버 ${v.cv_server}`;
  if ("ss_known" in v) return `ss 서버 ${v.ss_known ?? "-"} / 단말 ${v.ss_telemetry ?? "-"}`;
  if ("applied_crc" in v) return `적용 ${v.applied_crc} / 단말 ${v.device_crc}`;
  if ("md" in v) return `md ${v.md}`;
  return "";
}

export default function Alarms({ tick, onSelect }: { tick: number; onSelect: (uuid: string) => void }) {
  const [tab, setTab] = useState<AlarmTab | "all">("all");
  const [status, setStatus] = useState<"open" | "closed">("open");
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(DEFAULT_PAGE_SIZE);
  const [res, setRes] = useState<AlarmPage | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const id = setTimeout(() => (setQ(text.trim()), setPage(1)), 300);
    return () => clearTimeout(id);
  }, [text]);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.alarms({ status, tab: tab === "all" ? undefined : tab, q: q || undefined, page, size })
        .then((r) => alive && (setRes(r), setErr(null)))
        .catch((e) => alive && setErr(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [tab, status, q, page, size, tick]);

  const rows = res?.items ?? [];
  const total = res?.total ?? 0;
  return (
    <div className="content">
      <Card title="알람" className="full"
        meta={<span>서버가 30초마다 확인 · 해결되면 이력으로 넘어갑니다</span>}>
        <div className="tabs">
          {TABS.map((k) => (
            <button key={k} type="button" aria-pressed={tab === k} onClick={() => (setTab(k), setPage(1))}>
              {TAB_LABEL[k]}<b>{nf(res?.counts?.[k] ?? 0)}</b>
            </button>
          ))}
        </div>
        <div className="tool">
          <select value={status} aria-label="열림/이력" onChange={(e) => (setStatus(e.target.value as "open" | "closed"), setPage(1))}>
            <option value="open">열린 알람</option>
            <option value="closed">이력(해제됨)</option>
          </select>
          <input type="search" placeholder="시설명·UUID·주소" aria-label="알람 검색" value={text} onChange={(e) => setText(e.target.value)} style={{ width: 240 }} />
          <span className="sp" />
          <PageSize size={size} onChange={(n) => (setSize(n), setPage(1))} />
        </div>
        {err && <div className="err">{err}</div>}
        <div className="tw">
          <table className="list">
            <thead>
              <tr><th>등급</th><th>항목</th><th>시설명</th><th>지역 · 주소</th><th>UUID</th><th>발생</th><th>지속</th>{status === "closed" && <th>해제</th>}<th>마지막 값</th></tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id} data-click onClick={() => onSelect(a.uuid)} title="누르면 단말 상세">
                  <td><SeverityBadge s={a.severity} /></td>
                  <td><b>{a.label}</b>{a.tab && TAB_LABEL[a.tab] !== a.label ? <small className="muted"> {TAB_LABEL[a.tab]}</small> : null}</td>
                  <td>{a.site ?? <span className="muted">-</span>}</td>
                  <td title={a.address ?? ""}>{a.node_path?.split(">").slice(-2).join(" >") ?? ""}{a.address ? <div className="md">{a.address}</div> : null}</td>
                  <td className="mono">{a.uuid}</td>
                  <td title={a.first_seen_at}>{localTime(a.first_seen_at)}</td>
                  <td>{durText(a.duration_sec)}</td>
                  {status === "closed" && <td title={a.closed_at ?? ""}>{localTime(a.closed_at)}</td>}
                  <td title={alarmRaw(a) || undefined}>{alarmValue(a)}</td>
                </tr>
              ))}
              {rows.length === 0 && <tr><td colSpan={status === "closed" ? 9 : 8} className="empty">{res ? (status === "open" ? "열린 알람이 없습니다." : "이력이 없습니다.") : "불러오는 중…"}</td></tr>}
            </tbody>
          </table>
        </div>
        <Pager page={page} pages={Math.max(1, Math.ceil(total / size))} total={total} size={size} onPage={setPage} />
      </Card>
    </div>
  );
}
