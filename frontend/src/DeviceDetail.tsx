import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, Device, DeviceEvent, Telemetry, errorText } from "./api";
import { div100, erLabel, hex, localTime, mdLabel, pct, relTime, str } from "./format";

const REFRESH_MS = 10_000;
const PONG_WAIT_MS = 8_000;

interface Props {
  uuid: string;
  onDeleted: () => void;
}

export default function DeviceDetail({ uuid, onDeleted }: Props) {
  const [dev, setDev] = useState<Device | null>(null);
  const [tm, setTm] = useState<Telemetry[]>([]);
  const [ev, setEv] = useState<DeviceEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);

  // 설정 변경 폼
  const [ti, setTi] = useState("");
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [site, setSite] = useState("");
  const formTouched = useRef(false);

  const load = useCallback(async () => {
    try {
      const [d, t, e] = await Promise.all([api.getDevice(uuid), api.telemetry(uuid, 50), api.events(uuid, 50)]);
      setDev(d);
      setTm(t);
      setEv(e);
      setError(null);
      if (!formTouched.current) {
        setTi(String(d.ti_server ?? ""));
        setLat(d.lat === null ? "" : String(d.lat));
        setLon(d.lon === null ? "" : String(d.lon));
        setSite(d.site ?? "");
      }
    } catch (e) {
      setError(errorText(e));
    }
  }, [uuid]);

  useEffect(() => {
    formTouched.current = false;
    setDev(null);
    setActionMsg(null);
    setActionErr(null);
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  async function submitConfig(e: FormEvent) {
    e.preventDefault();
    setActionMsg(null);
    setActionErr(null);
    const body: { ti?: number; lat?: number; lon?: number; site?: string } = {};
    if (ti !== "") body.ti = Number(ti);
    if (lat !== "") body.lat = Number(lat);
    if (lon !== "") body.lon = Number(lon);
    if (site !== (dev?.site ?? "")) body.site = site;
    try {
      const r = await api.patchConfig(uuid, body);
      setActionMsg("설정 변경 응답: " + JSON.stringify(r));
      formTouched.current = false;
      load();
    } catch (err) {
      setActionErr(errorText(err));
    }
  }

  async function doPing() {
    setActionMsg(null);
    setActionErr(null);
    const since = Date.now() - 1000;
    try {
      const r = await api.ping(uuid);
      setActionMsg(`PING 발행 seq=${r.seq} — PONG 대기 중…`);
      const deadline = Date.now() + PONG_WAIT_MS;
      while (Date.now() < deadline) {
        await new Promise((res) => setTimeout(res, 1000));
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
    if (!confirm(`${uuid} 를 삭제할까요? (계정 포함, 이력은 남음)`)) return;
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

  return (
    <>
      <h3 className="mono" style={{ margin: "4px 0" }}>
        {dev.uuid} <span className={dev.is_online ? "on" : "off"}>{dev.is_online ? "● 온라인" : "○ 오프라인"}</span>{" "}
        <small>{dev.state}</small>
      </h3>
      {error && <div className="error">{error}</div>}

      <fieldset>
        <legend>기본정보</legend>
        <dl>
          <dt>state / reason</dt><dd>{dev.state} / {str(dev.state_reason)}</dd>
          <dt>has_mqtt_account</dt><dd>{str(dev.has_mqtt_account)}</dd>
          <dt>fw</dt><dd>{str(dev.fw)}</dd>
          <dt>device_model</dt><dd>{str(dev.device_model)}</dd>
          <dt>grp0 / grp1</dt><dd>{str(dev.grp0)} / {str(dev.grp1)}</dd>
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
        <legend>설정</legend>
        <dl>
          <dt>cv server / device</dt><dd>{dev.cv_server} / {str(dev.cv_device)} {dev.config_pending && <b className="error">(pending)</b>}</dd>
          <dt>ti server / device</dt><dd>{dev.ti_server} / {str(dev.ti_device)}</dd>
          <dt>lat / lon</dt><dd>{str(dev.lat)} / {str(dev.lon)}</dd>
          <dt>site</dt><dd>{str(dev.site)}</dd>
          <dt>config_sent_at</dt><dd>{localTime(dev.config_sent_at)}</dd>
        </dl>
      </fieldset>

      <fieldset>
        <legend>상태</legend>
        <dl>
          <dt>is_online / online(LWT)</dt><dd>{str(dev.is_online)} / {str(dev.online)}</dd>
          <dt>last_register_at</dt><dd>{localTime(dev.last_register_at)}</dd>
          <dt>last_telemetry_at</dt><dd>{localTime(dev.last_telemetry_at)} ({relTime(dev.last_telemetry_at)})</dd>
          <dt>last_seen_at</dt><dd>{localTime(dev.last_seen_at)}</dd>
          <dt>offline_at</dt><dd>{localTime(dev.offline_at)}</dd>
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
        <legend>설정 변경 (PATCH /config)</legend>
        <form onSubmit={submitConfig} className="toolbar" onChange={() => (formTouched.current = true)}>
          <label>ti <input type="number" min={60} max={3600} value={ti} onChange={(e) => setTi(e.target.value)} style={{ width: 70 }} /></label>
          <label>lat <input type="number" step="any" value={lat} onChange={(e) => setLat(e.target.value)} style={{ width: 100 }} /></label>
          <label>lon <input type="number" step="any" value={lon} onChange={(e) => setLon(e.target.value)} style={{ width: 100 }} /></label>
          <label>site <input value={site} onChange={(e) => setSite(e.target.value)} style={{ width: 100 }} /></label>
          <button type="submit">설정 변경</button>
          <button type="button" onClick={doPing}>PING</button>
          <button type="button" onClick={doDelete} style={{ color: "#b00" }}>삭제</button>
        </form>
        {actionMsg && <div className="mono">{actionMsg}</div>}
        {actionErr && <div className="error">{actionErr}</div>}
      </fieldset>

      <fieldset>
        <legend>최근 텔레메트리 ({tm.length})</legend>
        <table>
          <thead>
            <tr><th>received_at</th><th>sq</th><th>bv</th><th>bi</th><th>sc</th><th>pp</th><th>li</th><th>on</th><th>er</th></tr>
          </thead>
          <tbody>
            {tm.map((t, i) => (
              <tr key={i}>
                <td>{localTime(t.received_at)}</td><td>{str(t.sq)}</td>
                <td>{div100(t.bv)}</td><td>{div100(t.bi, "", true)}</td><td>{str(t.sc)}</td>
                <td>{div100(t.pp)}</td><td>{div100(t.li)}</td><td>{str(t.on)}</td><td>{erLabel(t.er)}</td>
              </tr>
            ))}
            {tm.length === 0 && <tr><td colSpan={9}>없음</td></tr>}
          </tbody>
        </table>
      </fieldset>

      <fieldset>
        <legend>이벤트 ({ev.length})</legend>
        <table>
          <thead>
            <tr><th>received_at</th><th>kind</th><th>payload</th></tr>
          </thead>
          <tbody>
            {ev.map((e) => (
              <tr key={e.id}>
                <td>{localTime(e.received_at)}</td><td>{e.kind}</td>
                <td className="mono" style={{ whiteSpace: "normal" }}>{JSON.stringify(e.payload)}</td>
              </tr>
            ))}
            {ev.length === 0 && <tr><td colSpan={3}>없음</td></tr>}
          </tbody>
        </table>
      </fieldset>
    </>
  );
}
