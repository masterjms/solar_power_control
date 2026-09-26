import { useEffect, useState } from "react";
import { api, DeviceList as DeviceListRes, STATES, errorText } from "./api";
import { div100, pct, relTime, str } from "./format";

const REFRESH_MS = 10_000;

interface Props {
  tick: number; // App 의 새로고침 버튼
  selected: string | null;
  onSelect: (uuid: string) => void;
}

/** 의도값 / 보고값 한 칸. 다르면 빨강(사양서 화면 1 "다르면 표시"). */
function Pair({ want, got }: { want: number | null | undefined; got: number | null | undefined }) {
  const diff = got !== null && got !== undefined && want !== got;
  return (
    <td className={diff ? "warn" : ""} title="서버 의도값 / 단말 보고값">
      {str(want)} / {str(got)}
    </td>
  );
}

/** 왼쪽 단말 목록(§3.9.2 화면 1). state/online/q 전부 서버 필터. 서버가 PENDING 을 먼저 준다. */
export default function DeviceList({ tick, selected, onSelect }: Props) {
  const [state, setState] = useState("");
  const [online, setOnline] = useState("");
  const [text, setText] = useState("");
  const [q, setQ] = useState(""); // 입력 후 300ms 지나면 text → q
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(50);
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
        .listDevices({ page, size, state: state || undefined, online: online || undefined, q: q || undefined })
        .then((r) => alive && (setRes(r), setError(null)))
        .catch((e) => alive && setError(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [page, size, state, online, q, tick]);

  const rows = res?.items ?? [];
  const total = res?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / size));

  return (
    <>
      <div className="toolbar">
        <select value={state} onChange={(e) => (setState(e.target.value), setPage(1))}>
          <option value="">state 전체</option>
          {STATES.map((s) => (
            <option key={s} value={s}>{s}{res?.counts ? ` (${res.counts[s]})` : ""}</option>
          ))}
        </select>
        <select value={online} onChange={(e) => (setOnline(e.target.value), setPage(1))}>
          <option value="">online 전체</option>
          <option value="true">온라인</option>
          <option value="false">오프라인</option>
        </select>
        <input placeholder="uuid / site 검색(서버)" value={text} onChange={(e) => setText(e.target.value)} />
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
            <th>UUID</th><th>상태</th><th>장소</th><th>F/W</th><th>마지막 수신</th><th>On</th>
            <th>프로필</th><th>ti 의도/보고</th><th>ka 의도/보고</th><th>bv</th><th>sc</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr
              key={d.uuid}
              data-click
              className={[d.uuid === selected ? "selected" : "", d.state === "PENDING" ? "pending" : ""].join(" ")}
              onClick={() => onSelect(d.uuid)}
            >
              <td className="mono">{d.uuid}</td>
              <td className={`st-${d.state}`}>{d.state}</td>
              <td>{str(d.site)}</td>
              <td>{str(d.fw)}</td>
              <td title={d.last_seen_at ?? ""}>{relTime(d.last_seen_at)}</td>
              <td
                className={d.is_online ? "on" : "off"}
                title={`is_online=${d.is_online} (브로커 online=${d.online}, 수신 보조 규칙 AND)`}
              >
                {d.is_online ? "●" : "○"}
              </td>
              <td title={`profile_id=${d.profile_id}`}>{str(d.profile_name)}</td>
              <Pair want={d.ti_effective} got={d.ti_device} />
              <Pair want={d.ka_effective} got={d.ka_device} />
              <td>{div100(d.last_telemetry?.bv, "V")}</td>
              <td>{pct(d.last_telemetry?.sc)}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={11}>표시할 단말 없음</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}
