import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  api, ConfigPatchBody, ConfigPatchRes, Device, DeviceEvent, DeviceState, Profile, Telemetry, errorText,
} from "./api";
import { div100, erLabel, eventSummary, hex, localTime, mdLabel, pct, relTime, str } from "./format";

const REFRESH_MS = 10_000;
const WATCH_POLL_MS = 5_000; // 승인/설정 후 "단말이 받음" 확인 폴링
const WATCH_MAX_MS = 10 * 60_000;
const PONG_WAIT_MS = 60_000; // 서버→단말 지연은 수십 초가 정상(§1.1.10)

interface Props {
  uuid: string;
  onChanged: () => void; // 요약바 갱신
  onDeleted: () => void;
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

export default function DeviceDetail({ uuid, onChanged, onDeleted }: Props) {
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
  const [bjd, setBjd] = useState("");
  const formTouched = useRef(false);
  const [cfgRes, setCfgRes] = useState<ConfigPatchRes | null>(null);
  const [cfgErr, setCfgErr] = useState<string | null>(null);

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
        setBjd(d.bjd_code ?? "");
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
      return { done: false, text: "대기 중 — retain 이라 다음 단말 송신·재접속 때 받음(직접 ACK 없음)" };
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
    const body: { state: DeviceState; site?: string; reason?: string | null } = { state: a.to };
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
    if (bjd !== (dev.bjd_code ?? "")) body.bjd_code = bjd || null;
    if (Object.keys(body).length === 0) { setCfgErr("바뀐 값 없음"); return; }
    try {
      const r = await api.patchConfig(uuid, body);
      setCfgRes(r);
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
        setActionMsg(`PING 발행 seq=${r.seq} — PONG 대기 중… (${Math.ceil((deadline - Date.now()) / 1000)}초 남음. 단말 송신 직후에만 도착하므로 수십 초는 정상)`);
        await new Promise((res) => setTimeout(res, 2000));
        const pongs = await api.events(uuid, 5, "PONG");
        const hit = pongs.find((p) => {
          const seq = (p.payload as { seq?: number } | null)?.seq;
          return Date.parse(p.received_at) >= since && (seq === undefined || seq === r.seq);
        });
        if (hit) {
          setActionMsg(`PONG 수신 (seq=${r.seq}, ${localTime(hit.received_at)}) ${JSON.stringify(hit.payload)}`);
          load();
          return;
        }
      }
      setActionMsg(`PING seq=${r.seq} — ${PONG_WAIT_MS / 1000}초 안에 PONG 없음 (타임아웃)`);
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

  if (error && !dev) return <div className="error">{error}</div>;
  if (!dev) return <p>불러오는 중…</p>;
  const lt = dev.last_telemetry;
  const selProfile = profiles.find((p) => p.id === Number(profileId));
  const tiEff = tiOv.trim() === "" ? selProfile?.ti ?? dev.ti_effective : Number(tiOv);
  const kaEff = kaOv.trim() === "" ? selProfile?.ka ?? dev.ka_effective : Number(kaOv);
  const recv = watch ? receivedText(watch, dev) : null;
  const watchExpired = watch && !recv?.done && Date.now() - watch.since > WATCH_MAX_MS;

  return (
    <>
      <h3 className="mono" style={{ margin: "4px 0" }}>
        {dev.uuid} <span className={dev.is_online ? "on" : "off"}>{dev.is_online ? "● 온라인" : "○ 오프라인"}</span>{" "}
        <span className={`badge st-${dev.state}`}>{dev.state}</span>
      </h3>
      {error && <div className="error">{error}</div>}

      {/* ---------- 승인 패널 (화면 2) ---------- */}
      <fieldset className={dev.state === "PENDING" ? "pending" : ""}>
        <legend>승인 상태 (PATCH /state)</legend>
        <dl>
          <dt>state</dt><dd><span className={`badge st-${dev.state}`}>{dev.state}</span> {dev.state === "PENDING" && <b className="warn">승인 대기</b>}</dd>
          <dt>state_reason</dt><dd>{str(dev.state_reason)}</dd>
          <dt>state_changed_at</dt><dd>{localTime(dev.state_changed_at)}</dd>
          <dt>마지막 REGISTER_ACK 발행</dt><dd>{localTime(dev.register_ack_at)} {dev.register_ack_at ? `(${relTime(dev.register_ack_at)})` : "(retain 없음)"}</dd>
        </dl>
        <div className="toolbar">
          <label>site <input value={ackSite} maxLength={24} placeholder="24자 이내" onChange={(e) => setAckSite(e.target.value)} style={{ width: 140 }} /></label>
          <label>reason <input value={ackReason} placeholder="거부·중지 사유(선택)" onChange={(e) => setAckReason(e.target.value)} style={{ width: 180 }} /></label>
        </div>
        <div className="toolbar">
          {ACTIONS[dev.state].map((a) => (
            <button key={a.to + a.label} type="button" onClick={() => doState(a)}
              className={a.to === "ACTIVE" ? "primary" : a.to === "RETIRED" || a.to === "REJECTED" ? "danger" : ""}>
              {a.label}
            </button>
          ))}
          <button type="button" onClick={doRepublish} title="DB 상태 그대로 REGISTER_ACK retain 재발행(RETIRED 면 빈 retain)">REGISTER_ACK 재발행</button>
        </div>
        {ackMsg && <div className="mono">{ackMsg}</div>}
        {ackErr && <div className="error">{ackErr}</div>}
        {watch?.kind === "state" && recv && (
          <div className="recv">
            <div>발행함: <b>{localTime(dev.register_ack_at) === "-" ? localTime(new Date(watch.since).toISOString()) : localTime(dev.register_ack_at)}</b></div>
            <div>단말이 받음: <b className={recv.done ? "on" : "warn"}>{recv.text}</b>{watchExpired && " (10분 지나 폴링 중단 — 새로고침으로 확인)"}</div>
          </div>
        )}
      </fieldset>

      {/* ---------- 설정 패널 ---------- */}
      <fieldset>
        <legend>설정 (PATCH /config)</legend>
        <dl>
          <dt>cv server / device</dt>
          <dd>
            {dev.cv_server} / {str(dev.cv_device)}{" "}
            {dev.config_pending && <b className="warn">CONFIG 미반영: cv_server {dev.cv_server} / cv_device {str(dev.cv_device)}</b>}
            {dev.cv_server === 0 && <small> (0 = 아직 보낸 적 없음)</small>}
          </dd>
          <dt>ti 의도 / 보고</dt><dd className={dev.ti_device !== null && dev.ti_device !== dev.ti_effective ? "warn" : ""}>{dev.ti_effective} / {str(dev.ti_device)}</dd>
          <dt>ka 의도 / 보고</dt><dd className={dev.ka_device !== null && dev.ka_device !== dev.ka_effective ? "warn" : ""}>{dev.ka_effective} / {str(dev.ka_device)}</dd>
          <dt>config_mismatch</dt><dd>{str(dev.config_mismatch)}</dd>
          <dt>config_sent_at</dt><dd>{localTime(dev.config_sent_at)}</dd>
          <dt>grp</dt><dd>{str(dev.grp)}</dd>
        </dl>
        <form onSubmit={submitConfig} onChange={() => (formTouched.current = true)}>
          <div className="toolbar">
            <label>프로필{" "}
              <select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>{p.name} (ti {p.ti} / ka {p.ka})</option>
                ))}
                {!profiles.some((p) => p.id === dev.profile_id) && <option value={dev.profile_id}>#{dev.profile_id} {dev.profile_name ?? ""}</option>}
              </select>
            </label>
            <label>ti_override <input type="number" min={60} max={3600} value={tiOv} placeholder="프로필 값" onChange={(e) => setTiOv(e.target.value)} style={{ width: 80 }} /> <small>→ 적용 {tiEff}</small></label>
            <label>ka_override <input type="number" min={60} max={1800} value={kaOv} placeholder="프로필 값" onChange={(e) => setKaOv(e.target.value)} style={{ width: 80 }} /> <small>→ 적용 {kaEff}</small></label>
          </div>
          <div className="toolbar">
            <label>lat <input type="number" step="any" value={lat} onChange={(e) => setLat(e.target.value)} style={{ width: 100 }} /></label>
            <label>lon <input type="number" step="any" value={lon} onChange={(e) => setLon(e.target.value)} style={{ width: 100 }} /></label>
            <label>site <input value={site} maxLength={24} onChange={(e) => setSite(e.target.value)} style={{ width: 140 }} /></label>
          </div>
          <div className="toolbar">
            <label>address <input value={address} onChange={(e) => setAddress(e.target.value)} style={{ width: 260 }} /></label>
            <label>bjd_code <input value={bjd} maxLength={10} placeholder="법정동 10자리" onChange={(e) => setBjd(e.target.value)} style={{ width: 110 }} /></label>
            <button type="submit" className="primary">설정 변경</button>
            <small>profile/override/lat/lon 이 바뀌면 cv_server +1 → CONFIG_SET. site/address/bjd 만 바꾸면 cv 그대로.</small>
          </div>
        </form>
        {cfgRes && (
          <div className="mono">
            응답: cv_server={cfgRes.cv_server} ti={cfgRes.ti_effective} ka={cfgRes.ka_effective} site={str(cfgRes.site)} published=<b className={cfgRes.published ? "on" : "warn"}>{String(cfgRes.published)}</b>
            {!cfgRes.published && cfgRes.reason === "NOT_ACTIVE" && <b className="warn"> — 승인 후 첫 Telemetry 때 전송됨</b>}
            {!cfgRes.published && cfgRes.reason && cfgRes.reason !== "NOT_ACTIVE" && <b className="warn"> — reason={cfgRes.reason}</b>}
            {cfgRes.payload !== undefined && cfgRes.payload !== null && <div>payload: {JSON.stringify(cfgRes.payload)}</div>}
          </div>
        )}
        {cfgErr && <div className="error">{cfgErr}</div>}
        {watch?.kind === "config" && recv && (
          <div className="recv">
            <div>발행함: <b>{cfgRes?.published ? localTime(dev.config_sent_at) : `(미발행 — ${dev.state === "ACTIVE" ? "브로커 미연결" : "승인 후 첫 Telemetry 때"})`}</b></div>
            <div>단말이 받음: <b className={recv.done ? "on" : "warn"}>{recv.text}</b>{watchExpired && " (10분 지나 폴링 중단)"}</div>
          </div>
        )}
      </fieldset>

      <fieldset>
        <legend>기본정보</legend>
        <dl>
          <dt>fw</dt><dd>{str(dev.fw)}</dd>
          <dt>device_model</dt><dd>{str(dev.device_model)}</dd>
          <dt>site / address</dt><dd>{str(dev.site)} / {str(dev.address)}</dd>
          <dt>bjd_code</dt><dd>{str(dev.bjd_code)}</dd>
          <dt>lat / lon</dt><dd>{str(dev.lat)} / {str(dev.lon)}</dd>
          <dt>created / updated</dt><dd>{localTime(dev.created_at)} / {localTime(dev.updated_at)}</dd>
        </dl>
      </fieldset>

      <fieldset>
        <legend>LTE · SIM</legend>
        <dl>
          <dt>modem_model</dt><dd>{str(dev.modem_model)}</dd>
          <dt>imei</dt><dd>{str(dev.imei)}</dd>
          <dt>iccid</dt><dd>{str(dev.iccid)}</dd>
          <dt>msisdn</dt><dd>{str(dev.msisdn)}</dd>
          <dt>ss_device</dt><dd>{str(dev.ss_device)}</dd>
        </dl>
      </fieldset>

      <fieldset>
        <legend>연결 상태</legend>
        <dl>
          <dt>is_online / online(브로커)</dt><dd>{str(dev.is_online)} / {str(dev.online)}</dd>
          <dt>online_changed_at</dt><dd>{localTime(dev.online_changed_at)}</dd>
          <dt>offline_at</dt><dd>{localTime(dev.offline_at)}</dd>
          <dt>last_register_at</dt><dd>{localTime(dev.last_register_at)}</dd>
          <dt>last_telemetry_at</dt><dd>{localTime(dev.last_telemetry_at)} ({relTime(dev.last_telemetry_at)})</dd>
          <dt>last_seen_at</dt><dd>{localTime(dev.last_seen_at)} ({relTime(dev.last_seen_at)})</dd>
          <dt>last_sq</dt><dd>{str(dev.last_sq)}</dd>
          <dt>lost / reboot</dt><dd>{dev.lost_count} / {dev.reboot_count}</dd>
        </dl>
      </fieldset>

      <fieldset>
        <legend>마지막 텔레메트리</legend>
        {lt ? (
          <table>
            <tbody>
              <tr><th>배터리 전압 bv</th><td>{div100(lt.bv, "V")}</td><th>배터리 전류 bi</th><td>{div100(lt.bi, "A", true)}</td></tr>
              <tr><th>충전량 sc</th><td>{pct(lt.sc)}</td><th>패널 출력 pp</th><td>{div100(lt.pp, "W")}</td></tr>
              <tr><th>부하 전류 li</th><td>{div100(lt.li, "A")}</td><th>점등 on</th><td>{str(lt.on)}</td></tr>
              <tr><th>모드 md</th><td>{mdLabel(lt.md)}</td><th>오류 er</th><td>{erLabel(lt.er)}</td></tr>
              <tr><th>cs</th><td>{hex(lt.cs)}</td><th>pw</th><td>{lt.pw ? lt.pw.join(", ") : "-"}</td></tr>
              <tr><th>sq / cv / ss</th><td>{str(lt.sq)} / {str(lt.cv)} / {str(lt.ss)}</td><th>ts / fw</th><td>{str(lt.ts ?? lt.ts_device)} / {str(lt.fw)}</td></tr>
            </tbody>
          </table>
        ) : (
          <p>없음 (아직 TM 수신 전)</p>
        )}
      </fieldset>

      <fieldset>
        <legend>PING · 삭제</legend>
        <div className="toolbar">
          <button type="button" onClick={doPing}>PING</button>
          <button type="button" onClick={doDelete} className="danger">삭제</button>
        </div>
        {actionMsg && <div className="mono">{actionMsg}</div>}
        {actionErr && <div className="error">{actionErr}</div>}
      </fieldset>

      <fieldset>
        <legend>최근 텔레메트리 ({tm.length})</legend>
        <table>
          <thead>
            <tr><th>received_at</th><th>sq</th><th>cv</th><th>bv</th><th>bi</th><th>sc</th><th>pp</th><th>li</th><th>on</th><th>er</th></tr>
          </thead>
          <tbody>
            {tm.map((t, i) => (
              <tr key={i}>
                <td>{localTime(t.received_at)}</td><td>{str(t.sq)}</td><td>{str(t.cv)}</td>
                <td>{div100(t.bv)}</td><td>{div100(t.bi, "", true)}</td><td>{str(t.sc)}</td>
                <td>{div100(t.pp)}</td><td>{div100(t.li)}</td><td>{str(t.on)}</td><td>{erLabel(t.er)}</td>
              </tr>
            ))}
            {tm.length === 0 && <tr><td colSpan={10}>없음</td></tr>}
          </tbody>
        </table>
      </fieldset>

      <fieldset>
        <legend>이벤트 ({ev.length})</legend>
        <table>
          <thead>
            <tr><th>received_at</th><th>kind</th><th>요약</th><th>payload</th></tr>
          </thead>
          <tbody>
            {ev.map((e) => (
              <tr key={e.id}>
                <td>{localTime(e.received_at)}</td>
                <td className={`ev-${e.kind}`}>{e.kind}</td>
                <td style={{ whiteSpace: "normal" }}>{eventSummary(e.kind, e.payload)}</td>
                <td className="mono" style={{ whiteSpace: "normal" }}>{JSON.stringify(e.payload)}</td>
              </tr>
            ))}
            {ev.length === 0 && <tr><td colSpan={4}>없음</td></tr>}
          </tbody>
        </table>
      </fieldset>
    </>
  );
}
