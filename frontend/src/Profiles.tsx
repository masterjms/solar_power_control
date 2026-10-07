import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Profile, errorText } from "./api";
import { Card, nf } from "./ui";

interface Props {
  onChanged: () => void;
}

/** 프로필: 표 안에서 name/ti/ka 편집·저장, 삭제, 오른쪽 카드에 추가 폼. */
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
        `"${e.name}" 저장됨` +
          (body.ti !== undefined || body.ka !== undefined
            ? ` — 단말 ${bumped}대에 각 단말의 다음 보고 때 반영됩니다`
            : " (이름만 바뀜 — 단말에는 보내지 않습니다)"),
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
    if (!confirm(`통신 주기 설정 "${p.name}"을(를) 삭제할까요? (쓰고 있는 단말이 있으면 지워지지 않습니다)`)) return;
    setMsg(null);
    setError(null);
    try {
      await api.deleteProfile(p.id);
      setMsg(`"${p.name}" 삭제됨`);
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
      setMsg(`"${r.name}" 추가됨`);
      setNName("");
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <>
      <Card title="통신 주기 설정" meta={`${rows.length}개`}>
        <div className="cap">
          보고 주기·접속 유지 주기. 바꾸면 이 설정을 쓰는 단말마다 다음 보고 때 반영됩니다. 기본 설정은 지울 수 없습니다.
        </div>
        {error && <div className="err">{error}</div>}
        {msg && <div className="okl">{msg}</div>}
        <div className="tw">
          <table className="list">
            <thead>
              <tr><th>이름</th><th>보고 주기(초)</th><th>접속 유지(초)</th><th>단말 수</th><th></th></tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const e = getEdit(p);
                return (
                  <tr key={p.id}>
                    <td><input value={e.name} onChange={(ev) => setField(p, "name", ev.target.value)} style={{ width: 220 }} /></td>
                    <td><input type="number" min={60} max={3600} value={e.ti} onChange={(ev) => setField(p, "ti", ev.target.value)} /></td>
                    <td><input type="number" min={60} max={1800} value={e.ka} onChange={(ev) => setField(p, "ka", ev.target.value)} /></td>
                    <td>{nf(p.device_count)}</td>
                    <td>
                      <div className="bar2" style={{ flexWrap: "nowrap" }}>
                        <button type="button" className={`btn sm ${dirty(p) ? "pri" : ""}`} disabled={!dirty(p)} onClick={() => save(p)}>저장</button>
                        <button type="button" className="btn sm danger" disabled={p.id === 1} onClick={() => del(p)}>삭제</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {rows.length === 0 && <tr><td colSpan={5} className="empty">없음</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="통신 주기 설정 추가">
        <form onSubmit={create} className="form2">
          <label className="w2">이름<input value={nName} required onChange={(e) => setNName(e.target.value)} placeholder="예: 2,200원 관제" /></label>
          <label>보고 주기(초)<input type="number" min={60} max={3600} value={nTi} onChange={(e) => setNTi(e.target.value)} /></label>
          <label>접속 유지(초)<input type="number" min={60} max={1800} value={nKa} onChange={(e) => setNKa(e.target.value)} /></label>
          <div className="w2"><button type="submit" className="btn pri">추가</button></div>
        </form>
      </Card>
    </>
  );
}
