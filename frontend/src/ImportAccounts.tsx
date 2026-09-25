import { FormEvent, useState } from "react";
import { api, errorText } from "./api";

interface Props {
  onDone: () => void;
}

/** 관리 탭: 단말 MQTT 계정 CSV import (uuid,password). */
export default function ImportAccounts({ onDone }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setResult(null);
    setError(null);
    try {
      const r = await api.importAccounts(file);
      setResult(JSON.stringify(r, null, 2));
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <fieldset>
      <legend>계정 import (POST /api/devices/import-accounts)</legend>
      <p>CSV: <code>uuid,password</code> (헤더 행 선택). 비밀번호에 ':' 와 공백 불가.</p>
      <form onSubmit={submit} className="toolbar">
        <input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button type="submit" disabled={!file || busy}>{busy ? "업로드 중…" : "업로드"}</button>
      </form>
      {error && <div className="error">{error}</div>}
      {result && <pre className="mono">{result}</pre>}
    </fieldset>
  );
}
