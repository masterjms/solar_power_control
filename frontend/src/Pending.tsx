import { useEffect, useState } from "react";
import { Device, api, errorText } from "./api";
import { localTime, relTime, str } from "./format";
import { RegionTree, isLeaf, pathOf, useRegions } from "./Tree";
import { Card, SiteHint, nf } from "./ui";

const REFRESH_MS = 10_000;
const SITE_MAX = 24; // 백엔드 한도(docs/05)

interface Props {
  tick: number;
  onChanged: () => void; // 요약(사이드바 배지) 갱신
  onSelect: (uuid: string) => void; // 드로어 열기
}

/** 승인하면 다음 Telemetry 때 나갈 CONFIG_SET 전체값(docs/05 PATCH /state 3단계 + /config 규칙). grp 는 여기 없다. */
function wouldBeConfig(d: Device) {
  const cv = d.cv_server > 0 ? d.cv_server : Math.max(1, (d.cv_device ?? 0) + 1);
  const p: Record<string, unknown> = { type: "CONFIG_SET", cv, ti: d.ti_effective, ka: d.ka_effective };
  if (d.lat !== null) p.lat = d.lat;
  if (d.lon !== null) p.lon = d.lon;
  return p;
}

/** 단말 등록·승인 — 목업 "승인 대기 단말" + #rdrawer(설치 위치 트리 + 시설명) + "승인하면 보내는 것". 등록은 REGISTER 로 자동. */
export default function Pending({ tick, onChanged, onSelect }: Props) {
  const [rows, setRows] = useState<Device[]>([]);
  const [total, setTotal] = useState(0);
  const [site, setSite] = useState<Record<string, string>>({});
  const [nodeOf, setNodeOf] = useState<Record<string, number | null>>({});
  const [sel, setSel] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [local, setLocal] = useState(0);
  const [filter, setFilter] = useState("");
  const { tree, list, error: treeErr } = useRegions(tick);

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
  }, [tick, local]);

  const selected = rows.find((d) => d.uuid === sel) ?? rows[0] ?? null;
  const siteOf = (d: Device) => site[d.uuid] ?? d.site ?? "";
  const nodeIdOf = (d: Device) => (d.uuid in nodeOf ? nodeOf[d.uuid] : d.node_id);
  const leafOf = (d: Device) => {
    const id = nodeIdOf(d);
    const n = id !== null && id !== undefined ? tree.byId.get(id) : undefined;
    return n && isLeaf(n) ? n : null;
  };

  async function approve(d: Device) {
    const s = siteOf(d).trim();
    const leaf = leafOf(d);
    if (!leaf) { setError("말단 법정동을 먼저 고른다"); return; }
    if (s.length > SITE_MAX) { setError(`시설명은 ${SITE_MAX}자 이내`); return; }
    setBusy(d.uuid);
    setMsg(null);
    setError(null);
    try {
      const r = await api.patchState(d.uuid, { state: "ACTIVE", node_id: leaf.r.id, ...(s ? { site: s } : {}) });
      setMsg(`${d.uuid} 승인 → REGISTER_ACK state=${r.state} site=${str(r.site)} grp=${str(leaf.r.grp)} published=${r.published}${r.published ? "" : " (브로커 미연결 — 드로어에서 재발행)"}. 첫 Telemetry 때 CONFIG_SET 이 나간다.`);
      setLocal((n) => n + 1);
      onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function reject(d: Device) {
    const reason = prompt(`${d.uuid}\n거부 사유(reason) — REGISTER_ACK 로 단말에도 내려감`);
    if (reason === null) return;
    setBusy(d.uuid);
    setMsg(null);
    setError(null);
    try {
      const r = await api.patchState(d.uuid, { state: "REJECTED", reason: reason.trim() || null });
      setMsg(`${d.uuid} 거부 → state=${r.state} published=${r.published}`);
      setLocal((n) => n + 1);
      onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  const leaf = selected ? leafOf(selected) : null;
  const s = selected ? siteOf(selected).trim() : "";
  const ack = selected
    ? { type: "REGISTER_ACK", uuid: selected.uuid, state: "ACTIVE", ...(s ? { site: s } : {}), ...(leaf?.r.grp ? { grp: leaf.r.grp } : {}) }
    : null;

  return (
    <div className="content">
      <Card title="승인 대기 단말" meta={`${nf(total)}대`}>
        <div className="cap">
          서버에 처음 접속(REGISTER)했지만 아직 승인하지 않은 단말이다. 승인 전에는 Telemetry 를 보내지 않고, 조명은 펌웨어 기본 스케줄로
          점등한다. 행을 고르고 오른쪽에서 <b>말단 법정동</b>과 <b>시설명</b>을 넣어 승인하면 REGISTER_ACK(retain, grp 포함)이 나간다.
        </div>
        {error && <div className="err">{error}</div>}
        {msg && <code className="payload">{msg}</code>}
        <div className="tw">
          <table className="list">
            <thead>
              <tr><th>UUID</th><th>모델</th><th>펌웨어</th><th>IMEI</th><th>최초 접속</th><th>마지막 수신</th><th>설치 위치</th><th>시설명</th><th></th></tr>
            </thead>
            <tbody>
              {rows.map((d) => {
                const lf = leafOf(d);
                return (
                  <tr key={d.uuid} data-click className={selected?.uuid === d.uuid ? "sel" : ""} onClick={() => setSel(d.uuid)}>
                    <td className="mono">{d.uuid}</td>
                    <td>{str(d.device_model)}</td>
                    <td>{str(d.fw)}</td>
                    <td className="mono">{str(d.imei)}</td>
                    <td title={d.created_at}>{localTime(d.created_at)}</td>
                    <td title={d.last_seen_at ?? ""}>{relTime(d.last_seen_at)}</td>
                    <td title={lf ? pathOf(lf) : ""}>{lf ? lf.r.name : <span className="muted">미선택</span>}</td>
                    <td>{siteOf(d) || <span className="muted">-</span>}</td>
                    <td>
                      <div className="bar2" style={{ flexWrap: "nowrap" }}>
                        <button type="button" className="btn sm danger" disabled={busy === d.uuid} onClick={(e) => (e.stopPropagation(), reject(d))}>거부</button>
                        <button type="button" className="btn sm" onClick={(e) => (e.stopPropagation(), onSelect(d.uuid))}>상세</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {rows.length === 0 && <tr><td colSpan={9} className="empty">승인 대기 단말이 없습니다.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="col">
        <Card title="승인" meta={selected ? <span className="mono">{selected.uuid}</span> : "행을 고른다"}>
          {selected ? (
            <>
              <div className="sec">
                <h4>설치 위치 <span>말단 법정동만 고를 수 있다</span></h4>
                <input type="search" placeholder="법정동 찾기" aria-label="트리 필터" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: "100%", marginBottom: 8 }} />
                {treeErr && <div className="err">{treeErr}</div>}
                <RegionTree
                  tree={tree}
                  selected={nodeIdOf(selected) ?? null}
                  onSelect={(v) => typeof v === "number" && setNodeOf((m) => ({ ...m, [selected.uuid]: v }))}
                  canSelect={isLeaf}
                  filter={filter}
                  className="short"
                  empty={list ? "지역이 없습니다. '지역(법정동)'에서 법정동을 먼저 추가한다." : "불러오는 중…"}
                />
                <div className="tpath">
                  {leaf ? <>선택: <b>{pathOf(leaf)}</b> · 법정동코드 <b>{str(leaf.r.bjd_code)}</b> · grp <b>{str(leaf.r.grp)}</b></> : "선택: 없음"}
                </div>
              </div>
              <div className="form2">
                <label className="w2">시설명 <SiteHint value={siteOf(selected)} />
                  <input value={siteOf(selected)} maxLength={SITE_MAX} placeholder="예: 산본동 22번 가로등"
                    onChange={(e) => setSite((m) => ({ ...m, [selected.uuid]: e.target.value }))} />
                </label>
              </div>
              <button type="button" className="btn pri send" disabled={busy === selected.uuid || !leaf} onClick={() => approve(selected)}>
                {leaf ? `${leaf.r.name}에 승인` : "말단 법정동을 골라야 승인할 수 있다"}
              </button>
            </>
          ) : (
            <div className="cap">대기 단말이 없다.</div>
          )}
        </Card>
        <Card title="승인하면 보내는 것" meta="승인 + 설치 정보">
          <ol className="steps">
            <li><span className="no">1</span><div><div>승인</div><div className="s"><code>REGISTER_ACK</code> retain 1 — state=ACTIVE, site, <b>grp</b>(법정동코드 + 00). 단말은 다음 접속·송신 때 받는다(§1.1.10)</div></div></li>
            <li><span className="no">2</span><div><div>첫 Telemetry</div><div className="s">단말이 ACTIVE 를 받고 보내는 첫 <code>TELEMETRY</code> 로 "받음" 확인</div></div></li>
            <li><span className="no">3</span><div><div>설정</div><div className="s">그 Telemetry 의 cv 가 서버와 다르면 <code>CONFIG_SET</code> 전체값(ti·ka·좌표) → <code>CONFIG_ACK</code>. grp 는 CONFIG_SET 에 넣지 않는다</div></div></li>
          </ol>
          {selected && ack ? (
            <>
              <div className="grid2">
                <div className="met"><div className="l">REGISTER_ACK (retain)</div><div className="v mono">{JSON.stringify(ack)}</div>{!leaf && <div className="h c-warn">법정동을 고르면 grp 가 붙는다</div>}</div>
                <div className="met"><div className="l">CONFIG_SET (첫 Telemetry 때)</div><div className="v mono">{JSON.stringify(wouldBeConfig(selected))}</div><div className="h">ti {selected.ti_effective} · ka {selected.ka_effective} · lat {str(selected.lat)} · lon {str(selected.lon)}</div></div>
              </div>
              <div className="cap">좌표·주소·프로필은 승인 전에도 상세 드로어의 설정 패널에서 넣을 수 있다(cv 는 승인 때 1 로).</div>
            </>
          ) : null}
        </Card>
        <Card title="단말 등록" meta="수동 등록">
          <div className="ph"><b>수동 등록 없음</b><span>단말은 첫 접속의 REGISTER 로 자동 등록되어 승인 대기에 온다.</span></div>
        </Card>
      </div>
    </div>
  );
}
