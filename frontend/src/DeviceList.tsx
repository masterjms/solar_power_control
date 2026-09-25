import { useEffect, useState } from "react";
import { api, Device, DeviceList as DeviceListRes, errorText } from "./api";
import { div100, pct, relTime, str } from "./format";

const STATES = ["PENDING", "ACTIVE", "SUSPENDED", "REJECTED", "RETIRED"];
const REFRESH_MS = 10_000;

interface Props {
  devices: Device[]; // App 이 집계용으로 읽은 전체 목록(여기선 안 씀, 갱신 트리거로만)
  selected: string | null;
  onSelect: (uuid: string) => void;
}

/** 왼쪽 장치 목록. state/online 은 서버 필터 + page/size, 텍스트는 현재 페이지 내 클라이언트 필터. */
export default function DeviceList({ devices, selected, onSelect }: Props) {
  const [state, setState] = useState("");
  const [online, setOnline] = useState("");
  const [text, setText] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(50);
  const [res, setRes] = useState<DeviceListRes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .listDevices({ page, size, state: state || undefined, online: online || undefined })
        .then((r) => alive && (setRes(r), setError(null)))
        .catch((e) => alive && setError(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [page, size, state, online, devices.length]);

  const q = text.trim().toLowerCase();
  const rows = (res?.items ?? []).filter(
    (d) => !q || d.uuid.toLowerCase().includes(q) || (d.site ?? "").toLowerCase().includes(q),
  );
  const total = res?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / size));

  return (
    <>
      <div className="toolbar">
        <select value={state} onChange={(e) => (setState(e.target.value), setPage(1))}>
          <option value="">state 전체</option>
          {STATES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <select value={online} onChange={(e) => (setOnline(e.target.value), setPage(1))}>
          <option value="">online 전체</option>
          <option value="true">온라인</option>
          <option value="false">오프라인</option>
        </select>
        <input placeholder="uuid / site 검색" value={text} onChange={(e) => setText(e.target.value)} />
        <select value={size} onChange={(e) => (setSize(Number(e.target.value)), setPage(1))}>
          {[20, 50, 100, 200, 500].map((n) => (
            <option key={n} value={n}>{n}개</option>
          ))}
        </select>
        <button disabled={page <= 1} onClick={() => setPage(page - 1)}>◀</button>
        <span>{page} / {pages} (총 {total})</span>
        <button disabled={page >= pages} onClick={() => setPage(page + 1)}>▶</button>
      </div>
      {error && <div className="error">{error}</div>}
      <table>
        <thead>
          <tr>
            <th>uuid</th><th>state</th><th>on</th><th>site</th><th>마지막 TM</th><th>bv</th><th>sc</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.uuid} data-click className={d.uuid === selected ? "selected" : ""} onClick={() => onSelect(d.uuid)}>
              <td className="mono">{d.uuid}</td>
              <td>{d.state}</td>
              <td className={d.is_online ? "on" : "off"}>{d.is_online ? "●" : "○"}</td>
              <td>{str(d.site)}</td>
              <td title={d.last_telemetry_at ?? ""}>{relTime(d.last_telemetry_at)}</td>
              <td>{div100(d.last_telemetry?.bv, "V")}</td>
              <td>{pct(d.last_telemetry?.sc)}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={7}>표시할 단말 없음</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}
