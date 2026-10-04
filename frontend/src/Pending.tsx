import { useEffect, useState } from "react";
import { Device, api, errorText } from "./api";
import { localTime, relTime, str } from "./format";
import { Card, nf } from "./ui";

const REFRESH_MS = 10_000;

interface Props {
  tick: number;
  onSelect: (uuid: string) => void; // 승인 전용 창 열기(App 의 드로어가 PENDING 이면 승인 창)
  /** 최고관리자만 대기 목록 전체를 본다. 지역관리자는 UUID 뒤 6자리 검색(문제점 21번). */
  canList?: boolean;
}

/** 지역관리자 — 단말에 붙은 UUID 뒤 6자리 이상을 넣어 찾는다. 1대면 승인 창, 여러 대면 개수만 알린다. */
function PendingSearch({ onSelect }: { onSelect: (uuid: string) => void }) {
  const [q, setQ] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const clean = q.trim().replace(/[^0-9a-fA-F]/g, "");
  async function find() {
    setMsg(null);
    if (clean.length < 6) return setMsg("뒤 6자리 이상을 넣는다(0~9, A~F)");
    setBusy(true);
    try {
      const r = await api.pendingSearch(clean);
      if (r.count === 1 && r.uuid) onSelect(r.uuid);
      else if (r.count === 0) setMsg("이 번호로 끝나는 승인 대기 단말이 없습니다. 단말이 서버에 한 번 접속했는지 확인하세요.");
      else setMsg(`${r.count}대가 같은 번호로 끝납니다 — 자리 수를 늘려(8자리 이상) 다시 찾으세요.`);
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="content">
      <Card title="단말 찾아 승인하기" className="full" meta="지역관리자">
        <div className="cap">단말 라벨의 UUID <b>뒤 6자리 이상</b>을 넣고 찾기를 누르면 승인 창이 열립니다. 승인할 때 맡은 시·도 안의 동을 고릅니다.</div>
        <div className="bar2" style={{ marginTop: 8, flexWrap: "nowrap" }}>
          <input className="inp mono" value={q} maxLength={24} placeholder="예: 04003A" style={{ flex: 1, maxWidth: 320 }}
            onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && find()} />
          <button type="button" className="btn pri" disabled={busy} onClick={find}>{busy ? "찾는 중…" : "찾기"}</button>
        </div>
        {msg && <div className="cap c-warn" style={{ marginTop: 8 }}>{msg}</div>}
      </Card>
    </div>
  );
}

/** 단말 등록·승인 — 승인 대기 목록만(문제점 #4). 행을 누르면 승인 전용 창에서 순서대로 진행한다.
 *  등록은 단말의 첫 REGISTER 로 자동. 승인·거절·폐기는 승인 창 한 곳에서만 한다. */
export default function Pending({ tick, onSelect, canList = true }: Props) {
  if (!canList) return <PendingSearch onSelect={onSelect} />;
  return <PendingList tick={tick} onSelect={onSelect} />;
}

function PendingList({ tick, onSelect }: { tick: number; onSelect: (uuid: string) => void }) {
  const [rows, setRows] = useState<Device[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .listDevices({ page: 1, size: 500, state: "PENDING" })
        .then((r) => {
          if (!alive) return;
          setRows(r.items);
          setTotal(r.total);
          setError(null);
        })
        .catch((e) => alive && setError(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [tick]);

  return (
    <div className="content">
      <Card title="승인 대기 단말" className="full" meta={`${nf(total)}대`}>
        <div className="cap">
          서버에 처음 접속(REGISTER)했지만 아직 승인하지 않은 단말이다. 승인 전에는 Telemetry 를 보내지 않고, 조명은 펌웨어 기본 스케줄로
          점등한다. 행을 누르면 <b>승인 창</b>이 열린다 — 단말기 정보 확인 → 설치 정보·지도 위치 → 통신 주기 설정 → 설정 변경 → 승인/거절/폐기.
        </div>
        {error && <div className="err">{error}</div>}
        <div className="tw">
          <table className="list">
            <thead>
              <tr><th>UUID</th><th>모델</th><th>펌웨어</th><th>IMEI</th><th>MSISDN</th><th>최초 접속</th><th>마지막 수신</th><th>지역</th><th>시설명</th></tr>
            </thead>
            <tbody>
              {rows.map((d) => (
                <tr key={d.uuid} data-click tabIndex={0} onClick={() => onSelect(d.uuid)} onKeyDown={(e) => e.key === "Enter" && onSelect(d.uuid)}>
                  <td className="mono">{d.uuid}</td>
                  <td>{str(d.device_model)}</td>
                  <td>{str(d.fw)}</td>
                  <td className="mono">{str(d.imei)}</td>
                  <td className="mono">{str(d.msisdn)}</td>
                  <td title={d.created_at}>{localTime(d.created_at)}</td>
                  <td title={d.last_seen_at ?? ""}>{relTime(d.last_seen_at)}</td>
                  <td title={d.node_path ?? ""}>{d.node_name ?? <span className="muted">미선택</span>}</td>
                  <td>{d.site || <span className="muted">-</span>}</td>
                </tr>
              ))}
              {rows.length === 0 && <tr><td colSpan={9} className="empty">승인 대기 단말이 없습니다.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
