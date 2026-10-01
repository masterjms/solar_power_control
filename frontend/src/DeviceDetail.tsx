import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  Alarm, api, ConfigPatchBody, ConfigPatchRes, Device, DeviceEvent, DeviceSchedule, DeviceState, Profile, Telemetry,
  errorText,
} from "./api";
import { SeverityBadge, alarmValue, durText as almDur } from "./Alarms";
import { dayEnergy, div100, erLabel, eventSummary, hex, localTime, mdLabel, pct, relTime, str } from "./format";
import { Battery, Lamp, Met, OnlineMark, SiteHint, StateBadge, SyncBadge } from "./ui";
import { CommandForm, CommandHistory, CommandResult, durText, useLedBasis } from "./Command";
import { RemoteBadge } from "./DeviceList";
import { RegionTree, isLeaf, pathOf, useRegions } from "./Tree";

const REFRESH_MS = 10_000;
const WATCH_POLL_MS = 5_000; // 승인/설정 후 "단말이 받음" 확인 폴링
const WATCH_MAX_MS = 10 * 60_000;
const PONG_WAIT_MS = 60_000; // 서버→단말 지연은 수십 초가 정상(§1.1.10)

interface Props {
  uuid: string;
  onChanged: () => void; // 요약바 갱신
  onDeleted: () => void;
  onClose: () => void;
}

/** docs/05 상태 전이표. 버튼 = 현재 state 에서 갈 수 있는 곳. */
interface Action {
  label: string;
  to: DeviceState;
  needSite?: boolean; // ACTIVE 로 갈 때 site 비어 있으면 묻는다
  needReason?: boolean; // 거부 사유
  confirm?: string;
}
const ACTIONS: Record<DeviceState, Action[]> = {
  PENDING: [
    { label: "승인 (ACTIVE)", to: "ACTIVE", needSite: true },
    { label: "거부 (REJECTED)", to: "REJECTED", needReason: true },
    { label: "폐기 (RETIRED)", to: "RETIRED", confirm: "폐기하면 REGISTER_ACK retain 을 지웁니다. 다시 쓰려면 재검토(PENDING)를 거쳐야 합니다." },
  ],
  ACTIVE: [
    { label: "일시 중지 (SUSPENDED)", to: "SUSPENDED" },
    { label: "승인 취소 (PENDING)", to: "PENDING" },
    { label: "폐기 (RETIRED)", to: "RETIRED", confirm: "운영 중인 단말을 폐기합니다. 계속할까요?" },
  ],
  SUSPENDED: [
    { label: "해제 (ACTIVE)", to: "ACTIVE", needSite: true },
    { label: "폐기 (RETIRED)", to: "RETIRED", confirm: "폐기할까요?" },
  ],
  REJECTED: [
    { label: "재검토 (PENDING)", to: "PENDING" },
    { label: "폐기 (RETIRED)", to: "RETIRED", confirm: "폐기할까요?" },
  ],
  RETIRED: [{ label: "재검토 (PENDING)", to: "PENDING" }],
};

/** 승인·설정 후 "단말이 받음" 을 기다리는 중인지. */
interface Watch {
  kind: "state" | "config";
  since: number; // 버튼 누른 시각(ms)
  target?: DeviceState;
  published: boolean;
}

function after(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false;
  return Date.parse(a) > Date.parse(b);
}

const evClass = (kind: string) =>
  ["REGISTER_ACK", "STATE_CHANGE", "CONFIG_SET", "PING"].includes(kind) ? "dn"
    : ["OFFLINE", "LWT", "LOST", "ERR"].includes(kind) ? "er" : "up";

/** 말단 법정동 고르기(드로어 설정 패널용). 펼 때만 트리를 읽는다. */
function NodePicker({ value, onPick }: { value: number | null; onPick: (id: number, label: string) => void }) {
  const { tree, list, error } = useRegions();
  const [filter, setFilter] = useState("");
  return (
    <div className="w2" style={{ display: "grid", gap: 8 }}>
      <input type="search" placeholder="법정동 찾기" aria-label="트리 필터" value={filter} onChange={(e) => setFilter(e.target.value)} />
      {error && <div className="err">{error}</div>}
      <RegionTree tree={tree} selected={value} onSelect={(v) => {
          if (typeof v !== "number") return;
          const n = tree.byId.get(v);
          onPick(v, n ? pathOf(n) : String(v));
        }} canSelect={isLeaf}
        filter={filter} className="short" empty={list ? "지역이 없습니다." : "불러오는 중…"} />
    </div>
  );
}

/** 단말 상세 드로어(§3.9.2 화면 2). 승인·설정·원격 제어·PING·삭제·이력 전부 여기. */
export default function DeviceDetail({ uuid, onChanged, onDeleted, onClose }: Props) {
  const [dev, setDev] = useState<Device | null>(null);
  const [tm, setTm] = useState<Telemetry[]>([]);
  const [ev, setEv] = useState<DeviceEvent[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [error, setError] = useState<string | null>(null);

  // 승인 패널
  const [ackSite, setAckSite] = useState("");
  const [ackReason, setAckReason] = useState("");
  const [ackMsg, setAckMsg] = useState<string | null>(null);
  const [ackErr, setAckErr] = useState<string | null>(null);
  const [watch, setWatch] = useState<Watch | null>(null);
  const [configAck, setConfigAck] = useState<DeviceEvent | null>(null);

  // 설정 패널
  const [profileId, setProfileId] = useState("");
  const [tiOv, setTiOv] = useState("");
  const [kaOv, setKaOv] = useState("");
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [site, setSite] = useState("");
  const [address, setAddress] = useState("");
  const [nodeSel, setNodeSel] = useState<number | null | undefined>(undefined); // undefined = 그대로
  const [nodeLabel, setNodeLabel] = useState<string | null>(null);
  const [picker, setPicker] = useState(false);
  const formTouched = useRef(false);
  const [cfgRes, setCfgRes] = useState<ConfigPatchRes | null>(null);
  const [cfgErr, setCfgErr] = useState<string | null>(null);

  // 원격 제어(5차)
  const [cmdSeq, setCmdSeq] = useState<number | null>(null);
  const [cmdNote, setCmdNote] = useState<string | null>(null);

  // PING / 삭제
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [d, t, e] = await Promise.all([api.getDevice(uuid), api.telemetry(uuid, 50), api.events(uuid, 50)]);
      setDev(d);
      setTm(t);
      setEv(e);
      setError(null);
      if (!formTouched.current) {
        setProfileId(String(d.profile_id));
        setTiOv(d.ti_override === null ? "" : String(d.ti_override));
        setKaOv(d.ka_override === null ? "" : String(d.ka_override));
        setLat(d.lat === null ? "" : String(d.lat));
        setLon(d.lon === null ? "" : String(d.lon));
        setSite(d.site ?? "");
        setAddress(d.address ?? "");
        setNodeSel(undefined);
        setNodeLabel(null);
        setAckSite(d.site ?? "");
      }
      return d;
    } catch (e) {
      setError(errorText(e));
      return null;
    }
  }, [uuid]);

  useEffect(() => {
    api.listProfiles().then(setProfiles).catch((e) => setError(errorText(e)));
  }, []);

  useEffect(() => {
    formTouched.current = false;
    setDev(null);
    setAckMsg(null);
    setAckErr(null);
    setWatch(null);
    setConfigAck(null);
    setCfgRes(null);
    setCfgErr(null);
    setActionMsg(null);
    setActionErr(null);
    setCmdSeq(null);
    setCmdNote(null);
    setPicker(false);
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  // ---- "단말이 받음" 판정 (렌더 시 계산) ----
  function receivedText(w: Watch, d: Device): { done: boolean; text: string } {
    if (!w.published) return { done: true, text: "발행 안 됨(브로커 미연결) — DB 만 저장. 'REGISTER_ACK 재발행' 또는 다음 REGISTER 때 자동" };
    if (w.kind === "state") {
      if (w.target === "RETIRED") return { done: true, text: "RETIRED retain 발행 후 빈 retain 으로 지움 — 단말은 다음 REGISTER 때 PENDING 으로 되돌아옴" };
      if (w.target === "ACTIVE") {
        if (after(d.last_telemetry_at, d.register_ack_at))
          return { done: true, text: `받음 — 첫 Telemetry ${localTime(d.last_telemetry_at)}${d.config_pending ? " (CONFIG_SET 은 이 Telemetry 때 나감 — cv 일치 대기)" : ""}` };
        return { done: false, text: "대기 중 — 다음 단말 송신(최대 5분) 후 첫 Telemetry 로 확인" };
      }
      // SUSPENDED / REJECTED / PENDING: 단말이 받아도 Telemetry 를 안 보낼 수 있다 → 아무 수신이나 기준(참고용)
      if (after(d.last_seen_at, d.register_ack_at))
        return { done: true, text: `이후 수신 있음 ${localTime(d.last_seen_at)} (retain 이라 재접속·다음 송신 때 받음. 직접 확인 수단 없음)` };
      return { done: false, text: "대기 중 — retain 이라 다음 단말 송신·재접속 때 받음(직접 응답 없음)" };
    }
    // config
    if (configAck) return { done: true, text: `CONFIG_ACK 받음 ${localTime(configAck.received_at)} — ${eventSummary("CONFIG_ACK", configAck.payload)}` };
    if (!d.config_pending && d.cv_device !== null && d.cv_device === d.cv_server)
      return { done: true, text: `받음 — Telemetry cv=${d.cv_device} 일치` };
    return { done: false, text: "대기 중 — CONFIG_ACK 또는 다음 Telemetry cv 로 확인" };
  }

  // 대기 중이면 5초마다 상세(+CONFIG_ACK) 폴링, 10분까지
  useEffect(() => {
    if (!watch || !dev) return;
    if (receivedText(watch, dev).done) return;
    if (Date.now() - watch.since > WATCH_MAX_MS) return;
    const id = setTimeout(async () => {
      if (watch.kind === "config") {
        try {
          const acks = await api.events(uuid, 5, "CONFIG_ACK");
          const hit = acks.find((a) => Date.parse(a.received_at) >= watch.since - 1000);
          if (hit) setConfigAck(hit);
        } catch { /* 다음 폴링 */ }
      }
      load();
    }, WATCH_POLL_MS);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watch, dev, configAck]);

  // ---- 승인/거부/중지/폐기/재검토 ----
  async function doState(a: Action) {
    setAckMsg(null);
    setAckErr(null);
    setConfigAck(null);
    const body: { state: DeviceState; site?: string; reason?: string | null; node_id?: number } = { state: a.to };
    // 설정 패널에서 말단을 골라 두었으면 승인과 함께 배정(docs/05 PATCH /state node_id)
    if (a.to === "ACTIVE" && typeof nodeSel === "number") body.node_id = nodeSel;
    if (a.needSite) {
      let s = ackSite.trim();
      if (!s) {
        const p = prompt("장소 이름(site, 24자 이내 — 단말 OLED 에 표시). 비우면 기존 값 유지", dev?.site ?? "");
        if (p === null) return;
        s = p.trim();
      }
      if (s.length > 24) { setAckErr("site 는 24자 이내"); return; }
      if (s) body.site = s;
    }
    if (a.needReason) {
      let r = ackReason.trim();
      if (!r) {
        const p = prompt("거부 사유(reason) — REGISTER_ACK 로 단말에도 내려감");
        if (p === null) return;
        r = p.trim();
      }
      body.reason = r || null;
    } else if (ackReason.trim()) {
      body.reason = ackReason.trim();
    }
    if (a.confirm && !confirm(`${uuid}\n${a.confirm}`)) return;
    try {
      const r = await api.patchState(uuid, body);
      setAckMsg(`${a.label} → 응답 state=${r.state} site=${str(r.site)} published=${r.published}`);
      setWatch({ kind: "state", since: Date.now(), target: r.state, published: r.published });
      setAckReason("");
      formTouched.current = false;
      await load();
      onChanged();
    } catch (e) {
      setAckErr(errorText(e));
    }
  }

  async function doRepublish() {
    setAckMsg(null);
    setAckErr(null);
    try {
      const r = await api.republishRegisterAck(uuid);
      setAckMsg("REGISTER_ACK 재발행: " + JSON.stringify(r));
      setWatch({ kind: "state", since: Date.now(), target: dev?.state, published: true });
      load();
    } catch (e) {
      setAckErr(errorText(e));
    }
  }

  // ---- 설정 변경 ----
  async function submitConfig(e: FormEvent) {
    e.preventDefault();
    setCfgRes(null);
    setCfgErr(null);
    setConfigAck(null);
    if (!dev) return;
    const body: ConfigPatchBody = {};
    const num = (s: string) => (s.trim() === "" ? null : Number(s));
    if (Number(profileId) !== dev.profile_id) body.profile_id = Number(profileId);
    if (num(tiOv) !== dev.ti_override) body.ti_override = num(tiOv);
    if (num(kaOv) !== dev.ka_override) body.ka_override = num(kaOv);
    if (num(lat) !== dev.lat) body.lat = num(lat);
    if (num(lon) !== dev.lon) body.lon = num(lon);
    if (site !== (dev.site ?? "")) {
      if (site.length > 24) { setCfgErr("site 는 24자 이내"); return; }
      body.site = site;
    }
    if (address !== (dev.address ?? "")) body.address = address || null;
    if (nodeSel !== undefined && nodeSel !== dev.node_id) body.node_id = nodeSel;
    if (Object.keys(body).length === 0) { setCfgErr("바뀐 값 없음"); return; }
    try {
      const r = await api.patchConfig(uuid, body);
      setCfgRes(r);
      setPicker(false);
      const affectsDevice = ["profile_id", "ti_override", "ka_override", "lat", "lon"].some((k) => k in body);
      if (affectsDevice) setWatch({ kind: "config", since: Date.now(), published: r.published || r.reason === "NOT_ACTIVE" });
      formTouched.current = false;
      await load();
      onChanged();
    } catch (err) {
      setCfgErr(errorText(err));
    }
  }

  // ---- PING ----
  async function doPing() {
    setActionMsg(null);
    setActionErr(null);
    const since = Date.now() - 1000;
    try {
      const r = await api.ping(uuid);
      const deadline = Date.now() + PONG_WAIT_MS;
      while (Date.now() < deadline) {
        setActionMsg(`PING 발행 (명령 번호 ${r.seq}) — PONG 대기 중… (${Math.ceil((deadline - Date.now()) / 1000)}초 남음. 단말 송신 직후에만 도착하므로 수십 초는 정상)`);
        await new Promise((res) => setTimeout(res, 2000));
        const pongs = await api.events(uuid, 5, "PONG");
        const hit = pongs.find((p) => {
          const seq = (p.payload as { seq?: number } | null)?.seq;
          return Date.parse(p.received_at) >= since && (seq === undefined || seq === r.seq);
        });
        if (hit) {
          setActionMsg(`PONG 수신 (명령 번호 ${r.seq}, ${localTime(hit.received_at)}) ${JSON.stringify(hit.payload)}`);
          load();
          return;
        }
      }
      setActionMsg(`PING (명령 번호 ${r.seq}) — ${PONG_WAIT_MS / 1000}초 안에 PONG 없음 (타임아웃)`);
    } catch (err) {
      setActionErr(errorText(err));
    }
  }

  async function doDelete() {
    if (!confirm(`${uuid} 를 삭제할까요? (행 삭제 + REGISTER_ACK retain 삭제, 이력은 남음)`)) return;
    try {
      const r = await api.deleteDevice(uuid);
      alert("삭제됨: " + JSON.stringify(r));
      onDeleted();
    } catch (err) {
      setActionErr(errorText(err));
    }
  }

  // LED 제어 밝기 = 설치 기준 대비 비율(문제점 19번). 조기 return 전에 부른다(훅 순서).
  const basis = useLedBasis(uuid, dev?.is_online ? dev.last_telemetry : null, dev?.last_telemetry_at);

  const header = (
    <div className="dh">
      <div style={{ minWidth: 0 }}>
        <h3>{dev ? (dev.site ?? "(장소 없음)") : "단말 상세"}</h3>
        <div className="u">{uuid}</div>
      </div>
      {dev && <StateBadge state={dev.state} />}
      {dev && <OnlineMark on={dev.is_online} title={`is_online=${dev.is_online} / 브로커 online=${dev.online}`} />}
      <button type="button" className="x" onClick={onClose} aria-label="닫기">✕</button>
    </div>
  );

  if (error && !dev) return <>{header}<div className="db"><div className="err">{error}</div></div></>;
  if (!dev) return <>{header}<div className="db"><div className="muted">불러오는 중…</div></div></>;
  const lt = dev.last_telemetry;
  const selProfile = profiles.find((p) => p.id === Number(profileId));
  const tiEff = tiOv.trim() === "" ? selProfile?.ti ?? dev.ti_effective : Number(tiOv);
  const kaEff = kaOv.trim() === "" ? selProfile?.ka ?? dev.ka_effective : Number(kaOv);
  const recv = watch ? receivedText(watch, dev) : null;
  const watchExpired = watch && !recv?.done && Date.now() - watch.since > WATCH_MAX_MS;

  return (
    <>
      {header}
      <div className="db">
        {error && <div className="err">{error}</div>}

        {/* ---------- 현재 상태 (목업 dMet) ---------- */}
        <div className="sec">
          <h4>현재 상태 <span>마지막 수신 {relTime(dev.last_seen_at)}</span></h4>
          <div className="grid2">
            <Met l="조명" v={<Lamp on={lt?.on} online={dev.is_online} />} h={lt ? mdLabel(lt.md) : "Telemetry 없음"} />
            <Met l="배터리" v={<Battery sc={lt?.sc} />} h={lt ? `${div100(lt.bv, "V")} · ${div100(lt.bi, "A", true)}` : ""} cls={lt && lt.sc !== undefined && lt.sc < 20 ? "a" : ""} />
            <Met l="패널 출력 / 부하 전류" v={lt ? `${div100(lt.pp, "W")} / ${div100(lt.li, "A")}` : "-"} h="부하 전류 = 부하 1 + 부하 2" />
            {/* 일일 전력량 — 단말(MPPT 보드)이 적산한 값(문제점 23번). 옛 펌웨어면 칸을 만들지 않는다. */}
            {lt && lt.eg !== undefined && (
              <>
                <Met l="오늘 발전 / 사용" v={`${dayEnergy(lt.eg, lt.er) || "-"} / ${dayEnergy(lt.eu, lt.er) || "-"}`} h="단말이 잰 값 · 자정에 0 으로" />
                <Met l="어제 발전 / 사용" v={`${dayEnergy(lt.yg, lt.er) || "-"} / ${dayEnergy(lt.yu, lt.er) || "-"}`} h="단말이 잰 값" />
              </>
            )}
            <Met l="오류 er" v={erLabel(lt?.er)} cls={lt?.er ? "a" : ""} />
            <Met l="통신" v={<OnlineMark on={dev.is_online} />} h={`브로커 online=${str(dev.online)} · lost ${dev.lost_count} · reboot ${dev.reboot_count}`} cls={dev.is_online ? "" : "o"} />
            <Met l="설정 버전 cv (서버/단말)" v={`${dev.cv_server} / ${str(dev.cv_device)}`} h={dev.config_pending ? "CONFIG 미반영" : dev.cv_server === 0 ? "아직 보낸 적 없음" : "일치"} cls={dev.config_pending ? "w" : ""} />
          </div>
        </div>

        {/* ---------- 승인 패널 (화면 2) ---------- */}
        <div className={`sec ${dev.state === "PENDING" ? "pending" : ""}`}>
          <h4>승인 상태 <span>PATCH /state</span></h4>
          <div className="grid2">
            <Met l="state" v={<StateBadge state={dev.state} />} h={dev.state === "PENDING" ? "승인 대기 — 승인 전에는 Telemetry 를 안 보낸다" : str(dev.state_reason)} />
            <Met l="마지막 REGISTER_ACK 발행" v={localTime(dev.register_ack_at)} h={dev.register_ack_at ? relTime(dev.register_ack_at) : "retain 없음"} />
            <Met l="state_changed_at" v={localTime(dev.state_changed_at)} />
            <Met l="state_reason" v={str(dev.state_reason)} />
          </div>
          <div className="form2" style={{ marginTop: 12 }}>
            <label>시설명 site <SiteHint value={ackSite} /><input value={ackSite} maxLength={24} placeholder="예: 산본동 22번 가로등" onChange={(e) => setAckSite(e.target.value)} /></label>
            <label>사유 reason <small>거부·중지 때 선택</small><input value={ackReason} placeholder="REGISTER_ACK 에 실림" onChange={(e) => setAckReason(e.target.value)} /></label>
          </div>
          <div className="cap" style={{ marginTop: 8 }}>
            지역: <b>{dev.node_path ?? "미배정"}</b>{dev.grp ? ` · grp ${dev.grp}` : ""}
            {typeof nodeSel === "number" && nodeSel !== dev.node_id && <> → <b className="c-blue">{nodeLabel}</b> (승인하면 같이 배정)</>}
            {!dev.node_id && typeof nodeSel !== "number" && (dev.state === "PENDING" || dev.state === "SUSPENDED") &&
              " — 승인하려면 법정동이 필요하다(NODE_REQUIRED). 아래 설정의 '지역'에서 고르거나 등록·승인 화면을 쓴다."}
          </div>
          <div className="bar2" style={{ marginTop: 12 }}>
            {ACTIONS[dev.state].map((a) => (
              <button key={a.to + a.label} type="button" onClick={() => doState(a)}
                className={`btn ${a.to === "ACTIVE" ? "pri" : a.to === "RETIRED" || a.to === "REJECTED" ? "danger" : ""}`}>
                {a.label}
              </button>
            ))}
            <button type="button" className="btn" onClick={doRepublish} title="DB 상태 그대로 REGISTER_ACK retain 재발행(RETIRED 면 빈 retain)">REGISTER_ACK 재발행</button>
          </div>
          {ackMsg && <code className="payload">{ackMsg}</code>}
          {ackErr && <div className="err" style={{ marginTop: 8 }}>{ackErr}</div>}
          {watch?.kind === "state" && recv && (
            <div className="recv">
              <div>발행함: <b>{localTime(dev.register_ack_at) === "-" ? localTime(new Date(watch.since).toISOString()) : localTime(dev.register_ack_at)}</b></div>
              <div>단말이 받음: <b className={recv.done ? "c-ok" : "c-warn"}>{recv.text}</b>{watchExpired && " (10분 지나 폴링 중단 — 새로고침으로 확인)"}</div>
            </div>
          )}
        </div>

        {/* ---------- 설정 패널 ---------- */}
        <div className="sec">
          <h4>설정 <span>PATCH /config</span></h4>
          <div className="grid2">
            <Met l="ti 의도 / 보고" v={`${dev.ti_effective} / ${str(dev.ti_device)}`} cls={dev.ti_device !== null && dev.ti_device !== dev.ti_effective ? "a" : ""} h="Telemetry 주기(초)" />
            <Met l="ka 의도 / 보고" v={`${dev.ka_effective} / ${str(dev.ka_device)}`} cls={dev.ka_device !== null && dev.ka_device !== dev.ka_effective ? "a" : ""} h="keepalive(초)" />
            <Met l="config_mismatch / pending" v={`${str(dev.config_mismatch)} / ${str(dev.config_pending)}`} cls={dev.config_mismatch || dev.config_pending ? "w" : ""} />
            <Met l="config_sent_at" v={localTime(dev.config_sent_at)} h={`grp ${str(dev.grp)}`} />
          </div>
          <form onSubmit={submitConfig} onChange={() => (formTouched.current = true)} className="form2" style={{ marginTop: 12 }}>
            <label className="w2">통신 주기 설정
              <select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>{p.name} (ti {p.ti} / ka {p.ka})</option>
                ))}
                {!profiles.some((p) => p.id === dev.profile_id) && <option value={dev.profile_id}>#{dev.profile_id} {dev.profile_name ?? ""}</option>}
              </select>
            </label>
            <label>ti_override <small>빈칸 = 주기 설정 값 → 적용 {tiEff}</small><input type="number" min={60} max={3600} value={tiOv} placeholder="주기 설정 값" onChange={(e) => setTiOv(e.target.value)} /></label>
            <label>ka_override <small>빈칸 = 주기 설정 값 → 적용 {kaEff}</small><input type="number" min={60} max={1800} value={kaOv} placeholder="주기 설정 값" onChange={(e) => setKaOv(e.target.value)} /></label>
            <label>위도 lat<input type="number" step="any" value={lat} onChange={(e) => setLat(e.target.value)} /></label>
            <label>경도 lon<input type="number" step="any" value={lon} onChange={(e) => setLon(e.target.value)} /></label>
            <label>시설명 site <SiteHint value={site} /><input value={site} maxLength={24} onChange={(e) => setSite(e.target.value)} /></label>
            <label>지역 (법정동) <small>bjd_code·grp 는 지역에서 자동</small>
              <span className="bar2" style={{ flexWrap: "nowrap" }}>
                <span className="mono" style={{ flex: 1, minWidth: 0 }} title={dev.node_path ?? ""}>
                  {nodeSel === undefined ? (dev.node_name ?? "미배정") : nodeSel === null ? "배정 해제" : nodeLabel}
                  {nodeSel === undefined && dev.bjd_code ? ` · ${dev.bjd_code}` : ""}
                </span>
                <button type="button" className="btn sm" onClick={() => setPicker((o) => !o)}>{picker ? "닫기" : "변경"}</button>
                {dev.node_id !== null && nodeSel !== null && (
                  <button type="button" className="btn sm" title="배정 해제(node_id: null)" onClick={() => { formTouched.current = true; setNodeSel(null); setNodeLabel(null); }}>해제</button>
                )}
              </span>
            </label>
            {picker && (
              <NodePicker value={typeof nodeSel === "number" ? nodeSel : dev.node_id}
                onPick={(id, label) => { formTouched.current = true; setNodeSel(id); setNodeLabel(label); }} />
            )}
            <label className="w2">주소 address<input value={address} onChange={(e) => setAddress(e.target.value)} /></label>
            <div className="w2 bar2">
              <button type="submit" className="btn pri">설정 변경</button>
              <span className="cap">profile/override/lat/lon 이 바뀌면 cv_server +1 → CONFIG_SET. site·지역이 바뀌면 REGISTER_ACK(retain) 재발행(site·grp), cv 그대로.</span>
            </div>
          </form>
          {cfgRes && (
            <code className="payload">
              응답: cv_server={cfgRes.cv_server} ti={cfgRes.ti_effective} ka={cfgRes.ka_effective} site={str(cfgRes.site)} published=<b className={cfgRes.published ? "c-ok" : "c-warn"}>{String(cfgRes.published)}</b>
              {!cfgRes.published && cfgRes.reason === "NOT_ACTIVE" && <b className="c-warn"> — 승인 후 첫 Telemetry 때 전송됨</b>}
              {!cfgRes.published && cfgRes.reason && cfgRes.reason !== "NOT_ACTIVE" && <b className="c-warn"> — reason={cfgRes.reason}</b>}
              {cfgRes.payload !== undefined && cfgRes.payload !== null && <details><summary>자세히</summary>보낸 내용: {JSON.stringify(cfgRes.payload)}</details>}
              {cfgRes.register_ack_republished && <div>REGISTER_ACK 재발행함(site·grp)</div>}
            </code>
          )}
          {cfgErr && <div className="err" style={{ marginTop: 8 }}>{cfgErr}</div>}
          {watch?.kind === "config" && recv && (
            <div className="recv">
              <div>발행함: <b>{cfgRes?.published ? localTime(dev.config_sent_at) : `(미발행 — ${dev.state === "ACTIVE" ? "브로커 미연결" : "승인 후 첫 Telemetry 때"})`}</b></div>
              <div>단말이 받음: <b className={recv.done ? "c-ok" : "c-warn"}>{recv.text}</b>{watchExpired && " (10분 지나 폴링 중단)"}</div>
            </div>
          )}
        </div>

        {/* ---------- 운전 설정 (S-23) — 값은 단말 설정 화면에서 ---------- */}
        <div className="sec">
          <h4>운전 설정 <span>밝기·다단계·배터리 보호·1년 스케줄 조건</span></h4>
          <div className="bar2">
            <SyncBadge sync={dev.settings_sync} />
            <span className="cap">
              {(dev.settings_sync ?? "unknown") === "unknown" ? "서버가 아직 이 단말 값을 모른다 — 단말 설정에서 먼저 읽는다"
                : dev.settings_sync === "local_saved" ? "현장에서 저장함 — 다시 읽어 확인"
                : dev.settings_sync === "device_changed" ? "단말 값이 DB 와 다름 — 받아들이기/되돌리기"
                : dev.settings_sync === "writing" ? "쓰는 중 — 단말 응답 대기" : "DB 값 = 단말 값"}
            </span>
            <span className="sp" />
            <a className="btn" href={`#config/${uuid}`} onClick={onClose} style={{ display: "inline-flex", alignItems: "center", textDecoration: "none" }}>단말 설정 열기</a>
          </div>
        </div>

        {/* ---------- 알람 (S-24) ---------- */}
        <DeviceAlarms uuid={uuid} />

        {/* ---------- 스케줄 (S-25, §13.1 ⑥) ---------- */}
        <DeviceScheduleSec uuid={uuid} />

        {/* ---------- 원격 제어 (5차 개별 COMMAND) ---------- */}
        <div className="sec">
          <h4>원격 제어 <span>개별 COMMAND · device/…/cmd · 채널마다 나중 명령이 이긴다(경로 무관)</span></h4>
          <div className="grid2">
            <Met l="운전 모드 md" v={mdLabel(lt?.md)} h={lt ? "Telemetry 기준" : "Telemetry 없음"} />
            <Met l="원격 조작" v={dev.remote_active ? `원격 ${Math.max(1, Math.ceil((dev.remote_remaining_sec ?? 0) / 60))}분 남음` : "없음"}
              h={dev.override_act ? `${dev.override_act}${dev.override_level ? ` · ${dev.override_level}` : ""} · 명령 번호 ${str(dev.override_seq)} · ~${localTime(dev.override_until)}` : "OK 응답 받은 명령 없음"}
              cls={dev.remote_active ? "w" : ""} />
          </div>
          {dev.remote_active && <div style={{ marginTop: 8 }}><RemoteBadge d={dev} onReleased={(m) => (setCmdNote(m), load())} /></div>}
          {cmdNote && <div className="okl" style={{ marginTop: 8 }}>{cmdNote}</div>}
          <div style={{ marginTop: 12 }}>
            <CommandForm target={{ kind: "device", id: uuid }} targetLabel={dev.site ?? uuid} basis={basis}
              blocked={dev.state !== "ACTIVE" ? "운영(ACTIVE) 단말에만 보낼 수 있다" : null}
              onSent={(c) => (setCmdSeq(c.seq), setCmdNote(`명령 #${c.seq} 발행함 ${localTime(c.sent_at)}${c.payload.dur ? ` · 유지 ${durText(Number(c.payload.dur))}` : ""}`))} />
          </div>
          {cmdSeq !== null && <CommandResult seq={cmdSeq} onClose={() => setCmdSeq(null)} />}
          <h4 style={{ marginTop: 12 }}>명령 이력 <span>이 단말이 대상에 든 명령</span></h4>
          <CommandHistory uuid={uuid} limit={20} selected={cmdSeq} onOpen={setCmdSeq} />
        </div>

        {/* ---------- PING · 삭제 ---------- */}
        <div className="sec">
          <h4>PING · 삭제 <span>PONG 은 단말 송신 직후에만 온다(60초 대기)</span></h4>
          <div className="bar2">
            <button type="button" className="btn" onClick={doPing}>PING</button>
            <button type="button" className="btn danger" onClick={doDelete}>삭제</button>
          </div>
          {actionMsg && <code className="payload">{actionMsg}</code>}
          {actionErr && <div className="err" style={{ marginTop: 8 }}>{actionErr}</div>}
        </div>

        {/* ---------- 단말기 정보 ---------- */}
        <details className="sec info">
          <summary>단말기 정보</summary>
          <div className="grid2">
            <Met l="모델" v={str(dev.device_model)} />
            <Met l="펌웨어" v={str(dev.fw)} />
            <Met l="모뎀" v={str(dev.modem_model)} h={`ss ${str(dev.ss_device)}`} />
            <Met l="IMEI" v={str(dev.imei)} />
            <Met l="ICCID" v={str(dev.iccid)} />
            <Met l="MSISDN" v={str(dev.msisdn)} />
            <Met l="시설명 / 주소" v={`${str(dev.site)} / ${str(dev.address)}`} />
            <Met l="법정동코드 / 좌표" v={`${str(dev.bjd_code)} / ${str(dev.lat)}, ${str(dev.lon)}`} />
            <Met l="online_changed_at" v={localTime(dev.online_changed_at)} h={`offline_at ${localTime(dev.offline_at)}`} />
            <Met l="last_register_at" v={localTime(dev.last_register_at)} />
            <Met l="last_telemetry_at" v={localTime(dev.last_telemetry_at)} h={relTime(dev.last_telemetry_at)} />
            <Met l="last_seen_at" v={localTime(dev.last_seen_at)} h={`last_sq ${str(dev.last_sq)}`} />
            <Met l="created / updated" v={localTime(dev.created_at)} h={localTime(dev.updated_at)} />
            {lt && <Met l="마지막 TM cs / pw" v={`${hex(lt.cs)} / ${lt.pw ? lt.pw.join(", ") : "-"}`} h={`sq ${str(lt.sq)} · cv ${str(lt.cv)} · ss ${str(lt.ss)} · ts ${str(lt.ts ?? lt.ts_device)} · fw ${str(lt.fw)}`} />}
          </div>
        </details>

        {/* ---------- 최근 텔레메트리 ---------- */}
        <div className="sec">
          <h4>최근 텔레메트리 <span>{tm.length}건</span></h4>
          <div style={{ overflowX: "auto" }}>
            <table className="mini">
              <thead>
                <tr><th>received_at</th><th className="n">sq</th><th className="n">cv</th><th className="n">bv</th><th className="n">bi</th><th className="n">sc</th><th className="n">pp</th><th className="n">li</th><th className="n">on</th><th>er</th></tr>
              </thead>
              <tbody>
                {tm.map((t, i) => (
                  <tr key={i}>
                    <td>{localTime(t.received_at)}</td><td className="n">{str(t.sq)}</td><td className="n">{str(t.cv)}</td>
                    <td className="n">{div100(t.bv)}</td><td className="n">{div100(t.bi, "", true)}</td><td className="n">{pct(t.sc)}</td>
                    <td className="n">{div100(t.pp)}</td><td className="n">{div100(t.li)}</td><td className="n">{str(t.on)}</td><td>{erLabel(t.er)}</td>
                  </tr>
                ))}
                {tm.length === 0 && <tr><td colSpan={10} className="muted">없음 (아직 TM 수신 전)</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        {/* ---------- 이벤트 ---------- */}
        <div className="sec">
          <h4>이벤트 <span>{ev.length}건 · config / result</span></h4>
          <ul className="log">
            {ev.map((e) => (
              <li key={e.id} title={JSON.stringify(e.payload)}>
                <span className="t">{localTime(e.received_at)}</span>
                <span className={evClass(e.kind)}>{e.kind}</span>
                <span className="body">{eventSummary(e.kind, e.payload)}</span>
              </li>
            ))}
            {ev.length === 0 && <li><span className="t">-</span><span /><span className="muted">없음</span></li>}
          </ul>
        </div>
      </div>
    </>
  );
}

/** 이 단말의 열린 알람 + 최근 이력(ADR-009). */
function DeviceAlarms({ uuid }: { uuid: string }) {
  const [rows, setRows] = useState<Alarm[] | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.deviceAlarms(uuid, 10).then((r) => alive && setRows(r)).catch(() => alive && setRows([]));
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [uuid]);
  const open = (rows ?? []).filter((a) => !a.closed_at);
  return (
    <div className="sec">
      <h4>알람 <span>열림 {open.length}건 · 최근 이력</span></h4>
      <table className="mini">
        <tbody>
          {(rows ?? []).map((a) => (
            <tr key={a.id}>
              <td><SeverityBadge s={a.severity} /></td>
              <td>{a.label}</td>
              <td className="mono">{alarmValue(a)}</td>
              <td>{localTime(a.first_seen_at)}</td>
              <td>{a.closed_at ? `해제 ${localTime(a.closed_at)}` : <b className="c-alarm">열림 · {almDur(a.duration_sec)}</b>}</td>
            </tr>
          ))}
          {rows && rows.length === 0 && <tr><td className="muted">알람 없음</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

/** 스케줄 탭(§13.1 ⑥) — 배정 프로필·단말 표·일치·오늘 점등/소등·DIP4, [단말에서 읽기] [다시 배포]. */
function DeviceScheduleSec({ uuid }: { uuid: string }) {
  const [s, setS] = useState<DeviceSchedule | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => api.deviceSchedule(uuid).then((r) => (setS(r), setErr(null))).catch((e) => setErr(errorText(e))), [uuid]);
  useEffect(() => {
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);
  async function act(fn: () => Promise<string>) {
    setErr(null);
    setMsg(null);
    try {
      setMsg(await fn());
      load();
    } catch (e) {
      setErr(errorText(e));
    }
  }
  if (!s) return <div className="sec"><h4>스케줄</h4><div className="muted">{err ?? "불러오는 중…"}</div></div>;
  return (
    <div className="sec">
      <h4>스케줄 <span>지정·적용·단말 표</span></h4>
      <div className="grid2">
        <Met l="지정 양식" v={s.profile_name ? `${s.profile_name} v${s.profile_version}` : "없음"} h={s.source === "device" ? "단말 예외" : s.source ? "지역에서 물려받음" : "그룹 스케줄 변경에서 지정"} />
        <Met l="적용" v={s.profile_id ? (s.applied_ok ? "적용됨" : s.applied_crc ? `옛 판 v${s.applied_version}` : "미적용") : "-"}
          h={s.applied_at ? localTime(s.applied_at) : ""} cls={s.profile_id && !s.applied_ok ? "w" : s.applied_ok ? "k" : ""} />
        <Met l="단말 표 crc (단말 / 양식)" v={<span className="mono">{s.device_crc ?? "모름"} / {s.profile_crc ?? "-"}</span>}
          h={s.device_region ? `${s.device_region} · src ${s.device_src}` : "단말에서 읽으면 보인다"}
          cls={s.device_crc && s.profile_crc && s.device_crc !== s.profile_crc ? "a" : ""} />
        <Met l="오늘 점등 ~ 소등" v={s.today_on ? `${s.today_on} ~ ${s.today_off}` : "-"} h="지정 양식 조건으로 서버 계산" />
      </div>
      {s.dip4 === false && <div className="cap c-warn">DIP4 OFF — 다단계를 무시하고 시작 밝기로만 운전한다.</div>}
      {s.deploy_status && <div className="cap">진행 중인 보내기 #{s.deploy_job_id}: {s.deploy_status}</div>}
      <div className="bar2" style={{ marginTop: 8 }}>
        <button type="button" className="btn" onClick={() => act(async () => { const r = await api.readSettings(uuid); return `단말에서 읽기 명령 번호 ${r.seq}`; })}>단말에서 읽기</button>
        <button type="button" className="btn pri" disabled={!s.profile_id || s.state !== "ACTIVE"} onClick={() => act(async () => {
          const j = await api.createDeploy({ profile_id: s.profile_id!, scope: "device", scope_id: uuid });
          return `보내기 #${j.id} 시작`;
        })}>다시 보내기</button>
        <a className="btn" href="#schedule" style={{ display: "inline-flex", alignItems: "center", textDecoration: "none" }}>그룹 스케줄 변경 화면</a>
      </div>
      {msg && <div className="okl">{msg}</div>}
      {err && <div className="err">{err}</div>}
    </div>
  );
}
