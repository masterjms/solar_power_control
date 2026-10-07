// 승인 전용 창(문제점 #4·#6). 승인 대기 단말을 단말 목록·등록·승인 어디서 골라도 이 창 하나로 온다.
// 위에서 아래로 한 번씩: 1 단말기 정보 → 2 설치 정보(시설명·지역·주소·지도 위치 보정) → 3 프로필
// → 4 설정 변경(저장) → 5 승인 상태(승인·거절·폐기). 운영 단말용 항목(원격 제어·PING 등)은 보이지 않는다.
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, Device, GeoResult, Profile, errorText } from "./api";
import { localTime, relTime, str } from "./format";
import { LocationPicker } from "./KakaoMap";
import { RegionTree, TreeNode, isLeaf, pathOf, useRegions } from "./Tree";
import { Met, OnlineMark, SiteHint, StateBadge, stateLabel } from "./ui";

const SITE_MAX = 24;

/** 저장·변경 안내에 보이는 항목 이름(필드명 대신). */
const FIELD_LABEL: Record<string, string> = {
  site: "시설명", node_id: "지역", address: "주소", lat: "위도", lon: "경도", profile_id: "통신 주기 설정",
};
const fieldNames = (keys: string[]) => keys.map((k) => FIELD_LABEL[k] ?? k).join(", ");

interface Props {
  uuid: string;
  onChanged: () => void; // 사이드바 배지·목록 갱신
  onClose: () => void;
}

function Step({ no, title, sub, done, children }: { no: number; title: string; sub?: string; done?: boolean; children: React.ReactNode }) {
  return (
    <div className="sec step">
      <h4><span className={`no ${done ? "done" : ""}`}>{done ? "✓" : no}</span>{title}{sub && <span>{sub}</span>}</h4>
      {children}
    </div>
  );
}

export default function Approval({ uuid, onChanged, onClose }: Props) {
  const [dev, setDev] = useState<Device | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [regionTick, setRegionTick] = useState(0);
  const { tree, list, error: treeErr } = useRegions(regionTick);

  // 편집값 — 저장 전까지 여기만 바뀐다
  const [site, setSite] = useState("");
  const [nodeId, setNodeId] = useState<number | null>(null);
  const [address, setAddress] = useState("");
  const [lat, setLat] = useState<number | null>(null);
  const [lon, setLon] = useState<number | null>(null);
  const [profileId, setProfileId] = useState<number | null>(null);
  const [, setTouched] = useState(false);

  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<GeoResult[] | null>(null);
  const [geoErr, setGeoErr] = useState<string | null>(null);
  const [suggest, setSuggest] = useState<GeoResult | null>(null); // 지도·검색이 알려 준 법정동
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [stateMsg, setStateMsg] = useState<string | null>(null);
  const [stateErr, setStateErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fill = useCallback((d: Device) => {
    setSite(d.site ?? "");
    setNodeId(d.node_id);
    setAddress(d.address ?? "");
    setLat(d.lat);
    setLon(d.lon);
    setProfileId(d.profile_id);
    setTouched(false);
  }, []);

  const load = useCallback(async (refill: boolean) => {
    try {
      const d = await api.getDevice(uuid);
      setDev(d);
      if (refill) fill(d);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [uuid, fill]);

  useEffect(() => {
    load(true);
    api.listProfiles().then(setProfiles).catch((e) => setError(errorText(e)));
  }, [load]);

  const leafByCode = useMemo(() => {
    const m = new Map<string, TreeNode>();
    tree.byId.forEach((n) => isLeaf(n) && n.r.bjd_code && m.set(n.r.bjd_code, n));
    return m;
  }, [tree]);

  /** 지도·주소 검색 결과 → 주소·좌표, 트리에 있는 법정동이면 바로 고른다. */
  function applyGeo(g: GeoResult, keepLatLon = false) {
    setTouched(true);
    setAddress(g.address_name);
    if (!keepLatLon && g.lat !== null && g.lon !== null) {
      setLat(g.lat);
      setLon(g.lon);
    }
    const leaf = leafByCode.get(g.bjd_code);
    if (leaf) {
      setNodeId(leaf.r.id);
      setSuggest(null);
    } else setSuggest(g);
  }

  async function search() {
    setGeoErr(null);
    setResults(null);
    if (query.trim().length < 2) { setGeoErr("두 글자 이상"); return; }
    try {
      const r = await api.geoSearch(query.trim());
      setResults(r);
      if (r.length === 1) applyGeo(r[0]);
    } catch (e) {
      setGeoErr(errorText(e));
    }
  }

  async function onPin(la: number, lo: number) {
    setTouched(true);
    setLat(la);
    setLon(lo);
    try {
      const g = await api.geoReverse(la, lo);
      if (g) applyGeo(g, true);
    } catch (e) {
      setGeoErr(`좌표 → 주소 변환 실패: ${errorText(e)} (좌표는 그대로 저장된다)`);
    }
  }

  async function addRegion(g: GeoResult) {
    try {
      const r = await api.createRegionFromAddress({ pick: g });
      setRegionTick((n) => n + 1);
      setTouched(true);
      setNodeId(r.id);
      setSuggest(null);
    } catch (e) {
      setGeoErr(errorText(e));
    }
  }

  if (error && !dev) return <><Head uuid={uuid} dev={null} onClose={onClose} /><div className="db"><div className="err">{error}</div></div></>;
  if (!dev) return <><Head uuid={uuid} dev={null} onClose={onClose} /><div className="db"><div className="muted">불러오는 중…</div></div></>;

  const leaf = nodeId !== null ? tree.byId.get(nodeId) : undefined;
  const changed: Record<string, unknown> = {};
  if (site.trim() !== (dev.site ?? "")) changed.site = site.trim();
  if (nodeId !== dev.node_id) changed.node_id = nodeId;
  if ((address.trim() || null) !== dev.address) changed.address = address.trim() || null;
  if (lat !== dev.lat) changed.lat = lat;
  if (lon !== dev.lon) changed.lon = lon;
  if (profileId !== null && profileId !== dev.profile_id) changed.profile_id = profileId;
  const dirty = Object.keys(changed).length > 0;
  const savedLeaf = dev.node_id !== null ? tree.byId.get(dev.node_id) : undefined;

  async function save() {
    setSaveMsg(null);
    setSaveErr(null);
    if (site.trim().length > SITE_MAX) { setSaveErr(`시설명은 ${SITE_MAX}자 이내`); return; }
    setBusy(true);
    try {
      await api.patchConfig(uuid, changed);
      setSaveMsg(`저장함 — ${fieldNames(Object.keys(changed))}`);
      await load(true);
      onChanged();
    } catch (e) {
      setSaveErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function setState(to: "ACTIVE" | "REJECTED" | "RETIRED") {
    setStateMsg(null);
    setStateErr(null);
    let reason: string | null = null;
    if (to === "REJECTED") {
      const p = prompt(`${dev!.site ?? uuid}\n거절 사유 — 승인 정보와 함께 단말에도 전달됩니다`);
      if (p === null) return;
      reason = p.trim() || null;
    }
    if (to === "RETIRED" && !confirm(`${dev!.site ?? uuid}\n폐기하면 단말의 승인 정보를 지웁니다. 계속할까요?`)) return;
    if (to === "ACTIVE" && !confirm(`${dev!.site ?? uuid}\n${savedLeaf ? pathOf(savedLeaf) : ""}\n승인합니다. 단말은 다음 보고 때 받습니다.`)) return;
    setBusy(true);
    try {
      const r = await api.patchState(uuid, { state: to, ...(reason ? { reason } : {}) });
      setStateMsg(`${to === "ACTIVE" ? "승인" : to === "REJECTED" ? "거절" : "폐기"}함 — ${r.published ? "단말에 승인 정보를 보냈습니다" : "단말 통신 서버와 연결이 끊겨 지금은 못 보냈습니다. 다음 접속 때 자동 전달됩니다"}`);
      onChanged();
      await load(false);
    } catch (e) {
      setStateErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const approveBlock = dirty ? "4단계 '설정 변경'으로 먼저 저장한다"
    : !dev.node_id ? "지역(법정동)을 골라 저장해야 승인할 수 있다"
    : !dev.site ? "시설명을 넣어 저장한다" : null;

  return (
    <>
      <Head uuid={uuid} dev={dev} onClose={onClose} />
      <div className="db appr">
        {error && <div className="err">{error}</div>}

        <Step no={1} title="단말기 정보" sub="처음 접속 때 단말이 보낸 값" done>
          <div className="grid2">
            <Met l="모델" v={str(dev.device_model)} />
            <Met l="펌웨어" v={str(dev.fw)} />
            <Met l="모뎀" v={str(dev.modem_model)} />
            <Met l="IMEI" v={<span className="mono">{str(dev.imei)}</span>} />
            <Met l="ICCID" v={<span className="mono">{str(dev.iccid)}</span>} />
            <Met l="MSISDN" v={<span className="mono">{str(dev.msisdn)}</span>} />
            <Met l="최초 접속" v={localTime(dev.created_at)} />
            <Met l="마지막 수신" v={relTime(dev.last_seen_at)} h={<OnlineMark on={dev.is_online} />} />
          </div>
        </Step>

        <Step no={2} title="설치 정보" sub="시설명 · 지역(법정동) · 주소 · 지도 위치" done={!!dev.site && !!dev.node_id}>
          <div className="form2">
            <label className="w2">시설명 <SiteHint value={site} />
              <input value={site} maxLength={SITE_MAX} placeholder="예: 산본동 22번 가로등" onChange={(e) => (setSite(e.target.value), setTouched(true))} />
            </label>
            <label className="w2">주소 검색 <small>카카오 주소 검색 — 고르면 주소·좌표·법정동이 채워진다</small>
              <span className="bar2" style={{ flexWrap: "nowrap" }}>
                <input value={query} placeholder="예: 경기도 군포시 금산로 91" onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), search())} style={{ flex: 1 }} />
                <button type="button" className="btn" onClick={search}>검색</button>
              </span>
            </label>
          </div>
          {geoErr && <div className="err">{geoErr}</div>}
          {results && results.length > 1 && (
            <ul className="georesults">
              {results.map((g) => (
                <li key={g.bjd_code + g.address_name}><button type="button" className="btn sm" onClick={() => (applyGeo(g), setResults(null))}>{g.address_name}</button> <small className="muted">{g.sido} {g.sigungu} {g.dong}</small></li>
              ))}
            </ul>
          )}
          {results && results.length === 0 && <div className="cap">검색 결과 없음</div>}
          <div className="form2" style={{ marginTop: 8 }}>
            <label className="w2">주소<input value={address} onChange={(e) => (setAddress(e.target.value), setTouched(true))} /></label>
          </div>
          <div className="cap" style={{ margin: "8px 0 4px" }}>지도 위치 보정 — 핀을 끌거나 지도를 누르면 좌표가 바뀌고 주소·법정동을 다시 찾는다.</div>
          <LocationPicker lat={lat} lon={lon} onMove={onPin} />
          <div className="cap">좌표 {lat === null ? "없음" : `${lat.toFixed(6)}, ${lon?.toFixed(6)}`}</div>
          {suggest && (
            <div className="confirm">
              이 자리의 법정동 <b>{suggest.sido} {suggest.sigungu} {suggest.dong}</b>({suggest.bjd_code})이 지역 트리에 없다.
              <div className="row"><button type="button" className="btn pri" onClick={() => addRegion(suggest)}>트리에 추가하고 고르기</button></div>
            </div>
          )}
          <h4 style={{ marginTop: 12 }}>지역 (법정동) <span>{leaf ? pathOf(leaf) : "미선택"}{leaf?.r.grp ? ` · 그룹 ${leaf.r.grp}` : ""}</span></h4>
          <input type="search" placeholder="법정동 찾기" aria-label="트리 필터" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: "100%", marginBottom: 8 }} />
          {treeErr && <div className="err">{treeErr}</div>}
          <RegionTree tree={tree} selected={nodeId} canSelect={isLeaf} filter={filter} className="short"
            onSelect={(v) => typeof v === "number" && (setNodeId(v), setTouched(true))}
            empty={list ? "지역이 없습니다. 주소 검색이나 지도로 법정동을 추가한다." : "불러오는 중…"} />
        </Step>

        <Step no={3} title="통신 주기 설정" sub="보고 주기 · 접속 유지 주기" done={profileId === dev.profile_id}>
          <select value={profileId ?? ""} onChange={(e) => (setProfileId(Number(e.target.value)), setTouched(true))} style={{ width: "100%" }}>
            {profiles.map((p) => <option key={p.id} value={p.id}>{p.name} (보고 {p.ti}초 / 접속 유지 {p.ka}초)</option>)}
            {!profiles.some((p) => p.id === dev.profile_id) && <option value={dev.profile_id}>#{dev.profile_id} {dev.profile_name ?? ""}</option>}
          </select>
        </Step>

        <Step no={4} title="설정 변경" sub="2·3단계에서 바꾼 것을 저장" done={!dirty}>
          <div className="bar2">
            <button type="button" className="btn pri" disabled={!dirty || busy} onClick={save}>설정 변경</button>
            {dirty && <button type="button" className="btn" onClick={() => fill(dev)}>되돌리기</button>}
            <span className="cap">{dirty ? `바뀜: ${fieldNames(Object.keys(changed))}` : "바뀐 것 없음"}</span>
          </div>
          {saveMsg && <div className="okl">{saveMsg}</div>}
          {saveErr && <div className="err">{saveErr}</div>}
        </Step>

        <Step no={5} title="승인 상태" sub={`지금: ${stateLabel(dev.state)}`} done={dev.state !== "PENDING"}>
          <div className="grid2">
            <Met l="상태" v={<StateBadge state={dev.state} />} h={dev.state === "PENDING" ? "승인 전에는 단말이 보고를 보내지 않습니다" : str(dev.state_reason)} />
            <Met l="설치 위치(저장된 값)" v={savedLeaf ? savedLeaf.r.name : "미배정"} h={`${str(dev.site)}${dev.grp ? ` · 그룹 ${dev.grp}` : ""}`} />
          </div>
          <div className="bar2" style={{ marginTop: 12 }}>
            <button type="button" className="btn pri" disabled={dev.state !== "PENDING" || !!approveBlock || busy} onClick={() => setState("ACTIVE")} title={approveBlock ?? ""}>승인</button>
            <button type="button" className="btn danger" disabled={dev.state !== "PENDING" || busy} onClick={() => setState("REJECTED")}>거절</button>
            <button type="button" className="btn danger" disabled={busy} onClick={() => setState("RETIRED")}>폐기</button>
          </div>
          {dev.state === "PENDING" && approveBlock && <div className="cap c-warn">승인 막힘: {approveBlock}</div>}
          {stateMsg && <div className="okl">{stateMsg}</div>}
          {stateErr && <div className="err">{stateErr}</div>}
        </Step>
      </div>
    </>
  );
}

function Head({ uuid, dev, onClose }: { uuid: string; dev: Device | null; onClose: () => void }) {
  return (
    <div className="dh">
      <div style={{ minWidth: 0 }}>
        <h3>단말 승인 {dev?.site ? `· ${dev.site}` : ""}</h3>
        <div className="u">{uuid}</div>
      </div>
      {dev && <StateBadge state={dev.state} />}
      <button type="button" className="x" onClick={onClose} aria-label="닫기">✕</button>
    </div>
  );
}
