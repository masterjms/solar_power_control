import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Profile, errorText } from "./api";

interface Props {
  onChanged: () => void;
}

/** 프로필 탭: 표 + 행 안에서 name/ti/ka 편집·저장, 삭제, 아래에 추가 폼. */
export default function Profiles({ onChanged }: Props) {
  const [rows, setRows] = useState<Profile[]>([]);
  const [edit, setEdit] = useState<Record<number, { name: string; ti: string; ka: string }>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nName, setNName] = useState("");
  const [nTi, setNTi] = useState("600");
  const [nKa, setNKa] = useState("300");

  const load = useCallback(async () => {
    try {
      setRows(await api.listProfiles());
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const getEdit = (p: Profile) => edit[p.id] ?? { name: p.name, ti: String(p.ti), ka: String(p.ka) };
  const setField = (p: Profile, k: "name" | "ti" | "ka", v: string) =>
    setEdit((m) => ({ ...m, [p.id]: { ...getEdit(p), [k]: v } }));
  const dirty = (p: Profile) => {
    const e = getEdit(p);
    return e.name !== p.name || Number(e.ti) !== p.ti || Number(e.ka) !== p.ka;
  };

  async function save(p: Profile) {
    setMsg(null);
    setError(null);
    const e = getEdit(p);
    const body: { name?: string; ti?: number; ka?: number } = {};
    if (e.name !== p.name) body.name = e.name;
    if (Number(e.ti) !== p.ti) body.ti = Number(e.ti);
    if (Number(e.ka) !== p.ka) body.ka = Number(e.ka);
    try {
      const r = await api.patchProfile(p.id, body);
      const bumped = r.bumped_devices ?? 0;
      setMsg(
        `#${p.id} 저장됨` +
          (body.ti !== undefined || body.ka !== undefined
            ? ` — ${bumped}대에 다음 송신 때 CONFIG 전송 (bumped_devices=${bumped})`
            : " (이름만 — 단말에 안 나감)"),
      );
      setEdit((m) => {
        const c = { ...m };
        delete c[p.id];
        return c;
      });
      await load();
      onChanged();
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function del(p: Profile) {
    if (!confirm(`프로필 #${p.id} "${p.name}" 삭제? (단말이 쓰고 있으면 409)`)) return;
    setMsg(null);
    setError(null);
    try {
      await api.deleteProfile(p.id);
      setMsg(`#${p.id} 삭제됨`);
      await load();
      onChanged();
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    setError(null);
    try {
      const r = await api.createProfile({ name: nName.trim(), ti: Number(nTi), ka: Number(nKa) });
      setMsg(`#${r.id} "${r.name}" 추가됨`);
      setNName("");
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <>
      <h3 style={{ margin: "4px 0" }}>설정 프로필 (/api/profiles)</h3>
      <p>
        <small>
          ti = Telemetry 주기(60~3600초), ka = keepalive(60~1800초). ti/ka 를 바꾸면 그 프로필의 단말 전부 cv_server +1
          → 각 단말의 다음 송신 때 CONFIG_SET 이 나간다(즉시 발행 아님, §1.1.10). id 1 은 삭제 불가.
        </small>
      </p>
      {error && <div className="error">{error}</div>}
      {msg && <div className="mono">{msg}</div>}
      <table style={{ width: "auto" }}>
        <thead>
          <tr><th>id</th><th>name</th><th>ti</th><th>ka</th><th>단말 수</th><th></th></tr>
        </thead>
        <tbody>
          {rows.map((p) => {
            const e = getEdit(p);
            return (
              <tr key={p.id}>
                <td>{p.id}</td>
                <td><input value={e.name} onChange={(ev) => setField(p, "name", ev.target.value)} style={{ width: 180 }} /></td>
                <td><input type="number" min={60} max={3600} value={e.ti} onChange={(ev) => setField(p, "ti", ev.target.value)} style={{ width: 70 }} /></td>
                <td><input type="number" min={60} max={1800} value={e.ka} onChange={(ev) => setField(p, "ka", ev.target.value)} style={{ width: 70 }} /></td>
                <td>{p.device_count}</td>
                <td>
                  <button type="button" disabled={!dirty(p)} onClick={() => save(p)}>저장</button>{" "}
                  <button type="button" disabled={p.id === 1} className="danger" onClick={() => del(p)}>삭제</button>
                </td>
              </tr>
            );
          })}
          {rows.length === 0 && <tr><td colSpan={6}>없음</td></tr>}
        </tbody>
      </table>
      <form onSubmit={create} className="toolbar" style={{ marginTop: 8 }}>
        <b>추가</b>
        <label>name <input value={nName} required onChange={(e) => setNName(e.target.value)} style={{ width: 180 }} /></label>
        <label>ti <input type="number" min={60} max={3600} value={nTi} onChange={(e) => setNTi(e.target.value)} style={{ width: 70 }} /></label>
        <label>ka <input type="number" min={60} max={1800} value={nKa} onChange={(e) => setNKa(e.target.value)} style={{ width: 70 }} /></label>
        <button type="submit" className="primary">추가</button>
      </form>
    </>
  );
}
