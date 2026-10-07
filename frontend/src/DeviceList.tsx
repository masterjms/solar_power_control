import { MouseEvent, useEffect, useState } from "react";
import { api, Device, DeviceList as DeviceListRes, STATES, errorText } from "./api";
import { localTime, relTime, str, volt1 } from "./format";
import { Battery, Card, LampPair, OnlineMark, StateBadge, stateLabel, nf, DEFAULT_PAGE_SIZE, PageSize, Pager } from "./ui";

const REFRESH_MS = 10_000;

interface Props {
  tick: number; // App 의 새로고침 버튼
  selected: string | null;
  onSelect: (uuid: string) => void;
}

/** 페이지 번호 버튼(목업 .pager). 현재 ±2 와 양 끝만. */
export { Pager };

/** 지역 경로를 짧게: "경기도 > 안양시 만안구 > 안양동" → "안양시 만안구 > 안양동". */
export function shortPath(d: Device): string {
  if (!d.node_path) return d.node_name ?? "-";
  const parts = d.node_path.split(">").map((x) => x.trim());
  return parts.slice(-2).join(" > ");
}

/** "원격 n분 남음" 배지 + 해제(act:auto 개별 명령). 사양서 §3.9.3 #7. */
export function RemoteBadge({ d, onReleased }: { d: Device; onReleased?: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  if (!d.remote_active) return <span className="muted">-</span>;
  const min = Math.max(1, Math.ceil((d.remote_remaining_sec ?? 0) / 60));
  async function release(e: MouseEvent) {
    e.stopPropagation();
    if (!confirm(`${d.site ?? d.uuid}\n원격 제어를 해제하고 스케줄로 복귀시킬까요?`)) return;
    setBusy(true);
    try {
      await api.createCommand({ target: { kind: "device", id: d.uuid }, act: "auto", ch: [1, 2] });
      onReleased?.(`${d.site ?? d.uuid}: 해제 명령을 보냈습니다 — 단말이 받기까지 최대 약 5분 걸릴 수 있습니다`);
    } catch (x) {
      onReleased?.(errorText(x));
    } finally {
      setBusy(false);
    }
  }
  return (
    <span className="bar2" style={{ flexWrap: "nowrap" }}>
      <span className="badge b-blue" title={`원격 조작 중 — ${min}분 뒤 스케줄로 돌아갑니다`}>원격 {min}분 남음</span>
      <button type="button" className="btn sm" disabled={busy || !d.is_online} title={d.is_online ? "원격 조작을 끝내고 스케줄로 복귀" : "오프라인 — 단말이 다시 접속하면 해제할 수 있습니다"} onClick={release}>해제</button>
    </span>
  );
}

/** 단말 목록(§3.9.2 화면 1, 문제점 #7·#8·#11). 기본은 운영(ACTIVE)만. state/online/q/remote 전부 서버 필터.
 *  검색은 한 글자·숫자 몇 개로도 된다(시설명·UUID·주소·지역 부분 일치). 행 클릭 → 드로어(대기 단말이면 승인 창). */
export default function DeviceList({ tick, selected, onSelect }: Props) {
  const [state, setState] = useState("ACTIVE");
  const [online, setOnline] = useState("");
  const [remote, setRemote] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [q, setQ] = useState(""); // 입력 후 300ms 지나면 text → q
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(DEFAULT_PAGE_SIZE);
  const [res, setRes] = useState<DeviceListRes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const id = setTimeout(() => (setQ(text.trim()), setPage(1)), 300);
    return () => clearTimeout(id);
  }, [text]);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .listDevices({ page, size, state: state || undefined, online: online || undefined, q: q || undefined, remote })
        .then((r) => alive && (setRes(r), setError(null)))
        .catch((e) => alive && setError(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [page, size, state, online, q, remote, tick]);

  const rows = res?.items ?? [];
  const total = res?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / size));

  return (
    <Card
      title="단말 목록"
      className="full"
      meta={<span>{res ? `${nf(total)}대 표시 · 전체 ${nf(STATES.reduce((a, s) => a + res.counts[s], 0))}대` : ""}</span>}
    >
      <div className="tool">
        <select value={state} aria-label="승인 상태" onChange={(e) => (setState(e.target.value), setPage(1))}>
          <option value="">전체 상태</option>
          {STATES.map((s) => (
            <option key={s} value={s}>{stateLabel(s)}{res?.counts ? ` (${res.counts[s]})` : ""}</option>
          ))}
        </select>
        <select value={online} aria-label="통신" onChange={(e) => (setOnline(e.target.value), setPage(1))}>
          <option value="">통신 전체</option>
          <option value="true">온라인</option>
          <option value="false">오프라인</option>
        </select>
        <label className="chk2" title="지금 원격 조작을 받고 있는 단말만 봅니다">
          <input type="checkbox" checked={remote} onChange={(e) => (setRemote(e.target.checked), setPage(1))} />원격 조작 중만
        </label>
        <input type="search" placeholder="시설명·UUID·주소·지역 (한 글자도 됨)" aria-label="검색" value={text} onChange={(e) => setText(e.target.value)} style={{ width: 260 }} />
        <span className="sp" />
        <PageSize size={size} onChange={(n) => (setSize(n), setPage(1))} />
      </div>
      {error && <div className="err">{error}</div>}
      {note && <div className="okl">{note}</div>}
      <div className="tw">
        <table className="list">
          <thead>
            <tr>
              <th>상태</th><th>시설명</th><th>지역</th><th>UUID</th><th>통신</th><th>조명</th><th>원격</th><th>배터리</th>
              <th>배터리 전압</th><th>F/W</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => (
              <tr key={d.uuid} data-click className={d.uuid === selected ? "sel" : ""} onClick={() => onSelect(d.uuid)}>
                <td><StateBadge state={d.state} /></td>
                <td>{str(d.site)}</td>
                <td title={d.node_path ?? "지역 미배정"}>{d.node_id ? shortPath(d) : <span className="muted">미배정</span>}</td>
                <td className="mono">{d.uuid}</td>
                <td title={`마지막 접속 ${d.last_seen_at ? localTime(d.last_seen_at) : "-"}`}>
                  <OnlineMark on={d.is_online} /> <span className="md">{relTime(d.last_seen_at)}</span>
                </td>
                <td><LampPair t={d.last_telemetry} online={d.is_online} /></td>
                <td><RemoteBadge d={d} onReleased={setNote} /></td>
                <td><Battery sc={d.last_telemetry?.sc} /></td>
                <td>{volt1(d.last_telemetry?.bv)}</td>
                <td>{str(d.fw)}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={10} className="empty">{res ? "조건에 맞는 단말이 없습니다. 필터를 바꿔 보세요." : "불러오는 중…"}</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager page={page} pages={pages} total={total} size={size} onPage={setPage} />
    </Card>
  );
}
