// #regions 지역(법정동) — 왼쪽 탐색기 트리, 오른쪽 선택 노드 + 법정동 추가(최고관리자).
// 사양서 §3.9.3 #1, docs/05 5차 "법정동 트리". 말단 추가는 카카오 주소 검색 → 결과 고르기 → from-address {pick}.
import { FormEvent, useEffect, useState } from "react";
import { ApiErrorException, Device, GeoResult, Health, Role, api, errorText } from "./api";
import { relTime, str } from "./format";
import { RegionTree, TreeSel, isLeaf, pathOf, useRegions } from "./Tree";
import { Card, OnlineMark, StateBadge, nf } from "./ui";

interface Props {
  role: Role | null;
  health: Health | null;
  tick: number;
  onSelect: (uuid: string) => void;
}

const LEVEL_LABEL: Record<string, string> = { sido: "시도", sigungu: "시군구", dong: "법정동(동)" };

export default function Regions({ role, health, tick, onSelect }: Props) {
  const { tree, list, error, reload } = useRegions(tick);
  const [sel, setSel] = useState<TreeSel>(null);
  const [filter, setFilter] = useState("");
  const isSuper = role === "super_admin";
  const node = typeof sel === "number" ? tree.byId.get(sel) ?? null : null;

  return (
    <div className="explorer">
      <Card title="지역 트리" meta={list ? `${nf(list.length)}개 지역 · 배정 단말 ${nf(tree.total)}대` : ""}>
        <input type="search" placeholder="이름 · 법정동코드로 찾기" aria-label="트리 필터" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {error && <div className="err">{error}</div>}
        <RegionTree tree={tree} selected={sel} onSelect={setSel} filter={filter} className="tall"
          empty={list ? "아직 지역이 없습니다. 오른쪽 '법정동 추가'로 만든다." : "불러오는 중…"} />
        <div className="cap">시도 &gt; 시군구 &gt; 법정동(동). 동만 그룹이다(group = 법정동코드 + 00). 숫자 = 그 아래 단말 수.</div>
      </Card>

      <div className="col">
        {node ? (
          <NodeCard key={node.r.id} nodeId={node.r.id} tree={tree} isSuper={isSuper} tick={tick} onSelect={onSelect}
            onChanged={reload} onDeleted={() => (setSel(null), reload())} />
        ) : (
          <Card title="선택한 지역"><div className="cap">왼쪽 트리에서 지역을 고른다.</div></Card>
        )}
        <AddRegion isSuper={isSuper} dev={health?.env === "dev"} onAdded={(id) => (reload(), setSel(id))} />
      </div>
    </div>
  );
}

function NodeCard({ nodeId, tree, isSuper, tick, onSelect, onChanged, onDeleted }: {
  nodeId: number; tree: ReturnType<typeof useRegions>["tree"]; isSuper: boolean; tick: number;
  onSelect: (uuid: string) => void; onChanged: () => void; onDeleted: () => void;
}) {
  const node = tree.byId.get(nodeId)!;
  const r = node.r;
  const leaf = isLeaf(node);
  const [name, setName] = useState(r.name);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [devs, setDevs] = useState<Device[] | null>(null);
  const [devTotal, setDevTotal] = useState(0);

  useEffect(() => setName(r.name), [r.name]);

  useEffect(() => {
    let alive = true;
    api
      .listDevices({ node_id: nodeId, size: 50 })
      .then((l) => alive && (setDevs(l.items), setDevTotal(l.total)))
      .catch((e) => alive && setErr(errorText(e)));
    return () => {
      alive = false;
    };
  }, [nodeId, tick]);

  async function rename(e: FormEvent) {
    e.preventDefault();
    const n = name.trim();
    if (!n || n === r.name) return;
    setMsg(null);
    setErr(null);
    try {
      await api.patchRegion(r.id, { name: n });
      setMsg("이름을 바꿨다.");
      onChanged();
    } catch (x) {
      setErr(errorText(x));
    }
  }

  async function remove() {
    if (!confirm(`${pathOf(node)} 을(를) 지울까요?\n하위 지역이나 배정된 단말이 있으면 지워지지 않는다(409).`)) return;
    setMsg(null);
    setErr(null);
    try {
      await api.deleteRegion(r.id);
      onDeleted();
    } catch (x) {
      setErr(errorText(x));
    }
  }

  return (
    <Card title={r.name} meta={LEVEL_LABEL[r.level] ?? r.level}>
      <div className="tpath">경로: <b>{pathOf(node)}</b></div>
      <div className="grid2">
        <div className="met"><div className="l">단말 (하위 전체)</div><div className="v">{nf(r.device_count)}대</div><div className="h">운영(ACTIVE) {nf(r.active_count)}대</div></div>
        <div className="met"><div className="l">{leaf ? "법정동코드 / group" : "하위"}</div>
          <div className="v mono">{leaf ? str(r.bjd_code) : `${nf(node.children.length)}개`}</div>
          <div className="h">{leaf ? `group ${str(r.grp)} → iotlight/group/${str(r.grp)}/cmd` : "동이 아니라 그룹이 아니다"}</div></div>
        <div className="met"><div className="l">좌표</div><div className="v">{r.lat !== null ? `${r.lat}, ${r.lon}` : "-"}</div><div className="h">"오늘 밤" 계산 기준(없으면 서울)</div></div>
        <div className="met"><div className="l">id</div><div className="v">{r.id}</div><div className="h">parent {str(r.parent_id)}</div></div>
      </div>
      {isSuper ? (
        <form className="bar2" onSubmit={rename}>
          <input value={name} onChange={(e) => setName(e.target.value)} aria-label="이름" style={{ flex: 1, minWidth: 160 }} />
          <button type="submit" className="btn" disabled={!name.trim() || name.trim() === r.name}>이름 바꾸기</button>
          <button type="button" className="btn danger" onClick={remove} disabled={node.children.length > 0 || r.device_count > 0}
            title={node.children.length > 0 || r.device_count > 0 ? "하위 지역이나 단말이 있으면 지울 수 없다" : ""}>삭제</button>
        </form>
      ) : (
        <div className="cap">이름 바꾸기·삭제·추가는 최고관리자만 한다.</div>
      )}
      {msg && <div className="okl">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <div className="sec">
        <h4>이 지역 단말 <span>{devs ? `${nf(devTotal)}대${devTotal > 50 ? " · 처음 50대" : ""}` : ""}</span></h4>
        <div style={{ overflowX: "auto" }}>
          <table className="mini">
            <thead><tr><th>상태</th><th>시설명</th><th>지역</th><th>통신</th><th>원격</th></tr></thead>
            <tbody>
              {(devs ?? []).map((d) => (
                <tr key={d.uuid} style={{ cursor: "pointer" }} onClick={() => onSelect(d.uuid)} title={d.uuid}>
                  <td><StateBadge state={d.state} /></td>
                  <td>{str(d.site)}</td>
                  <td>{str(d.node_name)}</td>
                  <td><OnlineMark on={d.is_online} /> <span className="md">{relTime(d.last_seen_at)}</span></td>
                  <td>{d.remote_active ? <span className="badge b-blue">원격 {Math.ceil((d.remote_remaining_sec ?? 0) / 60)}분</span> : <span className="muted">-</span>}</td>
                </tr>
              ))}
              {devs && devs.length === 0 && <tr><td colSpan={5} className="muted">배정된 단말이 없다.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </Card>
  );
}

/** 법정동 추가: 주소 검색 → 결과 고르기. 카카오 키가 없으면(503 GEO_UNAVAILABLE) 안내 + dev 에서만 직접 입력. */
function AddRegion({ isSuper, dev, onAdded }: { isSuper: boolean; dev: boolean; onAdded: (id: number) => void }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<GeoResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [geoDown, setGeoDown] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [man, setMan] = useState({ sido: "", sigungu: "", dong: "", bjd_code: "", lat: "", lon: "" });

  if (!isSuper) {
    return (
      <Card title="법정동 추가" meta="최고관리자">
        <div className="cap">트리 편집은 최고관리자만 한다(§3.9.3 #9).</div>
      </Card>
    );
  }

  async function search(e: FormEvent) {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      setRes(await api.geoSearch(q.trim()));
      setGeoDown(false);
    } catch (x) {
      if (x instanceof ApiErrorException && x.err.code === "GEO_UNAVAILABLE") setGeoDown(true);
      else setErr(errorText(x));
      setRes(null);
    } finally {
      setBusy(false);
    }
  }

  async function pick(g: GeoResult) {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await api.createRegionFromAddress({ pick: g });
      setMsg(`${g.sido} > ${g.sigungu} > ${r.name} (${str(r.bjd_code)}) — 추가됨(이미 있으면 그 지역)`);
      onAdded(r.id);
    } catch (x) {
      setErr(errorText(x));
    } finally {
      setBusy(false);
    }
  }

  async function manual(e: FormEvent) {
    e.preventDefault();
    if (!/^\d{10}$/.test(man.bjd_code)) { setErr("법정동코드는 숫자 10자리"); return; }
    if (!man.sido.trim() || !man.sigungu.trim() || !man.dong.trim()) { setErr("시도·시군구·법정동을 모두 넣는다"); return; }
    const body: { sido: string; sigungu: string; dong: string; bjd_code: string; lat?: number; lon?: number } = {
      sido: man.sido.trim(), sigungu: man.sigungu.trim(), dong: man.dong.trim(), bjd_code: man.bjd_code,
    };
    if (man.lat.trim() && man.lon.trim()) {
      body.lat = Number(man.lat);
      body.lon = Number(man.lon);
    }
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await api.createRegionFromAddress(body);
      setMsg(`${body.sido} > ${body.sigungu} > ${r.name} (${str(r.bjd_code)}) — 추가됨`);
      onAdded(r.id);
    } catch (x) {
      setErr(errorText(x));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="법정동 추가" meta="카카오 주소 검색 → 고르기">
      <form className="bar2" onSubmit={search}>
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="예: 경기도 군포시 금산로 91 / 안양동" aria-label="주소 검색" style={{ flex: 1, minWidth: 200 }} />
        <button type="submit" className="btn pri" disabled={busy || !q.trim()}>검색</button>
      </form>
      <div className="cap">결과를 고르면 시도·시군구·법정동을 찾거나 만들고(상위까지), 동을 트리에 넣는다. 코드를 손으로 치지 않는다.</div>
      {res && (
        <ul className="picklist">
          {res.map((g, i) => (
            <li key={`${g.bjd_code}-${i}`}>
              <div>
                <div>{g.address_name}</div>
                <div className="cap">{g.sido} &gt; {g.sigungu} &gt; <b>{g.dong}</b> · <span className="mono">{g.bjd_code}</span></div>
              </div>
              <button type="button" className="btn sm pri" disabled={busy} onClick={() => pick(g)}>추가</button>
            </li>
          ))}
          {res.length === 0 && <li className="muted">검색 결과 없음</li>}
        </ul>
      )}
      {geoDown && (
        <div className="confirm">
          주소 검색을 쓸 수 없다(503 <code>GEO_UNAVAILABLE</code> — 서버에 카카오 키 <code>KAKAO_REST_API_KEY</code> 없음).
          {dev ? " 개발 환경이라 아래에 직접 넣을 수 있다." : " 운영자에게 키 설정을 요청한다."}
        </div>
      )}
      {geoDown && dev && (
        <form className="form2" onSubmit={manual}>
          <label>시도<input value={man.sido} onChange={(e) => setMan({ ...man, sido: e.target.value })} placeholder="경기도" /></label>
          <label>시군구<input value={man.sigungu} onChange={(e) => setMan({ ...man, sigungu: e.target.value })} placeholder="안양시 만안구" /></label>
          <label>법정동<input value={man.dong} onChange={(e) => setMan({ ...man, dong: e.target.value })} placeholder="안양동" /></label>
          <label>법정동코드 <small>10자리</small><input value={man.bjd_code} maxLength={10} inputMode="numeric" onChange={(e) => setMan({ ...man, bjd_code: e.target.value.replace(/\D/g, "") })} placeholder="4117110100" /></label>
          <label>위도 <small>선택</small><input value={man.lat} inputMode="decimal" onChange={(e) => setMan({ ...man, lat: e.target.value })} /></label>
          <label>경도 <small>선택</small><input value={man.lon} inputMode="decimal" onChange={(e) => setMan({ ...man, lon: e.target.value })} /></label>
          <div className="w2 bar2">
            <button type="submit" className="btn pri" disabled={busy}>직접 추가 (dev)</button>
            <span className="cap">APP_ENV=dev 에서만 서버가 받는다.</span>
          </div>
        </form>
      )}
      {msg && <div className="okl">{msg}</div>}
      {err && <div className="err">{err}</div>}
    </Card>
  );
}
