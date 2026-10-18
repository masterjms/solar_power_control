// #ota OTA 관리(문제점 56번, 사양서 §24) — 최고관리자. "그룹 스케줄 변경" 아래 메뉴.
// 왼쪽 법정동 트리(말단만 고름), 오른쪽 펌웨어 파일·대상 단말·보내기, 아래 보낸 기록(단말별 결과).
// 대상은 단말 하나 또는 법정동 하나(시·구 일괄 불가). 그룹 전에 1대 먼저 시험하도록 안내한다.
import { FormEvent, useCallback, useEffect, useState } from "react";
import { OtaDevice, OtaItem, OtaJob, OtaOverview, OtaPackage, OtaStatus, api, errorText } from "./api";
import { localTime, relTime } from "./format";
import { RegionTree, TreeSel, isLeaf, pathOf, useRegions } from "./Tree";
import { Card, OnlineMark, Pager, nf, pageCount } from "./ui";

const ST: Record<OtaStatus, [string, string]> = {
  waiting: ["대기", "b-off"],
  sending: ["보냄 · 응답 기다림", "b-blue"],
  running: ["받는 중", "b-blue"],
  DONE: ["성공", "b-ok"],
  ROLLBACK: ["되돌림(실패)", "b-alarm"],
  BAD: ["시작 안 함 · 형식", "b-warn"],
  STATE: ["시작 안 함 · 승인 아님", "b-warn"],
  BATT: ["시작 안 함 · 저전압", "b-warn"],
  BUSY: ["시작 안 함 · 바쁨", "b-warn"],
  HTTP: ["실패 · 다운로드", "b-alarm"],
  SIZE: ["실패 · 크기", "b-alarm"],
  HASH: ["실패 · 파일 검사", "b-alarm"],
  FLASH: ["실패 · 저장", "b-alarm"],
  NO_RESPONSE: ["응답 없음", "b-warn"],
  CANCELLED: ["취소", "b-off"],
  SKIPPED: ["안 보냄 · 운영 아님", "b-off"],
};
const OPEN: OtaStatus[] = ["waiting", "sending", "running"];
const FAIL: OtaStatus[] = ["ROLLBACK", "BAD", "STATE", "BATT", "BUSY", "HTTP", "SIZE", "HASH", "FLASH", "NO_RESPONSE"];

function Badge({ s }: { s: OtaStatus }) {
  const [t, c] = ST[s] ?? [s, "b-off"];
  return <span className={`badge ${c}`} title={s}>{t}</span>;
}

const kb = (n: number) => `${(n / 1024).toFixed(1)}KB`;
const sum = (c: Partial<Record<OtaStatus, number>>, ks: OtaStatus[]) => ks.reduce((a, k) => a + (c[k] ?? 0), 0);

function Upload({ fwMax, onDone }: { fwMax: number; onDone: (msg: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [version, setVersion] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [key, setKey] = useState(0);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setErr(null);
    try {
      const p = await api.otaUpload(file, version, note);
      onDone(`올렸습니다: ${p.filename} → pkg ${nf(p.size)} 글자, SHA-256 ${p.sha256.slice(0, 12)}…`);
      setFile(null); setVersion(""); setNote(""); setKey((k) => k + 1);
    } catch (x) {
      setErr(errorText(x));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="form2" onSubmit={submit}>
      <label className="w2">펌웨어 파일
        <input key={key} type="file" accept=".bin" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <small>빌드 결과 SolarLTE.bin 그대로(최대 {kb(fwMax)}). 서버가 base64 한 줄로 바꾸고 SHA-256 을 계산합니다.</small></label>
      <label>버전<input value={version} placeholder="예 1.4.1" maxLength={40} onChange={(e) => setVersion(e.target.value)} />
        <small>단말이 보고하는 펌웨어 버전(참고용)</small></label>
      <label>메모<input value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} />
        <small>바뀐 점 등</small></label>
      <div className="w2 bar2">
        <button type="submit" className="btn pri" disabled={busy || !file}>{busy ? "올리는 중…" : "파일 올리기"}</button>
        {err && <span className="err">{err}</span>}
      </div>
    </form>
  );
}

function JobDetail({ id, onClose, onSelect, onChanged }: { id: number; onClose: () => void; onSelect: (u: string) => void; onChanged: () => void }) {
  const [job, setJob] = useState<(OtaJob & { items: OtaItem[] }) | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => api.otaJob(id).then((j) => (setJob(j), setErr(null))).catch((e) => setErr(errorText(e))), [id]);
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);
  if (!job) return <div className="muted">{err ?? "불러오는 중…"}</div>;
  const waiting = job.counts.waiting ?? 0;
  const failed = sum(job.counts, FAIL);
  return (
    <div className="cmdres">
      <div className="bar2">
        <b>OTA #{job.id}</b> <span>{job.filename}{job.version ? ` (${job.version})` : ""} · {job.scope_label}</span>
        <span className="sp" />
        <button type="button" className="btn sm" disabled={failed === 0 || !job.package_id} onClick={async () => {
          if (!confirm(`실패·응답 없음 ${failed}대에 같은 파일을 다시 보낼까요? (새 기록으로 만듭니다)`)) return;
          try { const r = await api.otaRetry(job.id); setMsg(`새 기록 #${r.id}로 ${r.total}대 다시 보냄`); onChanged(); } catch (e) { setErr(errorText(e)); }
        }}>실패·응답 없음만 다시 ({failed})</button>
        <button type="button" className="btn sm danger" disabled={waiting === 0} onClick={async () => {
          if (!confirm(`아직 시작하지 않은 ${waiting}대를 취소할까요? 이미 받는 중인 단말은 멈출 수 없습니다(끝까지 진행).`)) return;
          try { const r = await api.otaCancel(job.id); setMsg(`대기 ${r.cancelled}대 취소`); load(); onChanged(); } catch (e) { setErr(errorText(e)); }
        }}>대기 취소</button>
        <button type="button" className="btn sm" onClick={onClose}>닫기</button>
      </div>
      {msg && <div className="okl">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <div className="cap">
        보냄 = 단말 응답(START) 기다림, 30초씩 3회. 받는 중 = 단말이 MQTT 를 끊고 받는 중(통신 두절 알람을 올리지 않음) → 재부팅 → 성공이면 "성공".
        시작 뒤 1시간 30분 결과가 없으면 "응답 없음"(현장 확인). 받은 조각 수 = 단말의 다운로드 요청 수(약 48번).
      </div>
      <div style={{ maxHeight: 380, overflow: "auto" }}>
        <table className="mini">
          <thead><tr><th>단말</th><th>통신</th><th>상태</th><th className="n">보낸 횟수</th><th className="n">받은 조각</th><th>시작</th><th>끝</th><th>펌웨어(전 → 후)</th><th>설명</th></tr></thead>
          <tbody>
            {job.items.map((i) => (
              <tr key={i.id} data-click onClick={() => onSelect(i.uuid)}>
                <td>{i.site ?? "-"} <small className="mono muted">{i.uuid.slice(-8)}</small></td>
                <td><OnlineMark on={i.online} /></td>
                <td><Badge s={i.status} /></td>
                <td className="n">{i.attempts}</td>
                <td className="n">{i.downloads || ""}</td>
                <td>{localTime(i.started_at ?? i.sent_at)}</td>
                <td>{localTime(i.finished_at)}</td>
                <td className="mono">{i.fw_before ?? "-"}{i.fw_after ? ` → ${i.fw_after}` : ""}</td>
                <td className="wrap">{i.detail ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Ota({ tick, onSelect }: { tick: number; onSelect: (uuid: string) => void }) {
  const { tree, list, error: treeErr } = useRegions(tick);
  const [sel, setSel] = useState<TreeSel>(null);
  const [filter, setFilter] = useState("");
  const [ov, setOv] = useState<OtaOverview | null>(null);
  const [pkgs, setPkgs] = useState<OtaPackage[] | null>(null);
  const [pkgId, setPkgId] = useState<number | null>(null);
  const [devs, setDevs] = useState<OtaDevice[] | null>(null);
  const [uuid, setUuid] = useState("");
  const [jobs, setJobs] = useState<OtaJob[]>([]);
  const [jobTotal, setJobTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [job, setJob] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [n, setN] = useState(0);
  const refresh = () => setN((x) => x + 1);
  const node = typeof sel === "number" ? tree.byId.get(sel) ?? null : null;
  const pkg = pkgs?.find((p) => p.id === pkgId) ?? null;

  useEffect(() => {
    api.otaOverview().then(setOv).catch((e) => setErr(errorText(e)));
    api.otaPackages().then((p) => { setPkgs(p); setPkgId((cur) => cur ?? p[0]?.id ?? null); }).catch((e) => setErr(errorText(e)));
    api.otaJobs(page).then((r) => (setJobs(r.items), setJobTotal(r.total))).catch((e) => setErr(errorText(e)));
  }, [tick, n, page]);
  useEffect(() => {
    if (!node || !isLeaf(node)) { setDevs(null); return; }
    api.otaDevices({ node_id: node.r.id }).then((r) => setDevs(r.items)).catch((e) => setErr(errorText(e)));
  }, [node?.r.id, tick, n]); // eslint-disable-line react-hooks/exhaustive-deps

  const winText = ov ? (ov.hour_start === ov.hour_end ? "하루 종일" : `${ov.hour_start}~${ov.hour_end}시`) : "";
  const warnText = "재부팅·맞바꾸기 동안 LED 가 약 20~30초 꺼집니다.";
  const late = ov && !ov.window_open ? `\n\n지금은 진행 시간대(${winText}) 밖입니다 — 대기로 넣고 시간대가 되면 시작합니다.` : "";

  async function send(kind: "device" | "node", id: string, label: string, count: number) {
    if (!pkg) return;
    let text = `${pkg.filename}${pkg.version ? ` (${pkg.version})` : ""} 를 ${label} ${count}대에 보낼까요?\n\n${warnText} 오프라인 단말은 대기했다가 접속하면 시작합니다.${late}`;
    if (kind === "node" && pkg.done === 0) {
      text = `이 파일로 성공한 단말이 아직 없습니다.\n단말 1대에 먼저 보내 "성공"을 확인한 뒤 법정동 전체에 보내는 것을 권장합니다.\n\n그래도 ${label} ${count}대에 보낼까요?${late}`;
    }
    if (!confirm(text)) return;
    setErr(null);
    try {
      const r = await api.otaCreateJob({ package_id: pkg.id, kind, id });
      setMsg(`OTA #${r.id} — ${r.total}대${r.skipped_busy.length ? ` (이미 OTA 중이라 뺀 단말 ${r.skipped_busy.length}대)` : ""}`);
      setJob(r.id);
      setPage(1);
      refresh();
    } catch (x) {
      setErr(errorText(x));
    }
  }

  async function sendUuid() {
    const u = uuid.trim().toUpperCase();
    try {
      const r = await api.otaDevices({ uuid: u });
      const d = r.items[0];
      if (!d) return setErr("그 UUID 의 단말이 없습니다");
      await send("device", d.uuid, d.site ? `${d.site}(${d.uuid.slice(-6)})` : d.uuid, 1);
    } catch (x) {
      setErr(errorText(x));
    }
  }

  const active = devs?.filter((d) => d.state === "ACTIVE") ?? [];
  return (
    <div className="explorer">
      <Card title="대상 (법정동)" meta={list ? `법정동 ${nf(list.filter((r) => r.level === "dong").length)}개` : ""}>
        <input type="search" placeholder="이름 · 법정동코드로 찾기" aria-label="트리 필터" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {treeErr && <div className="err">{treeErr}</div>}
        <RegionTree tree={tree} selected={sel} onSelect={setSel} canSelect={isLeaf} filter={filter} className="tall"
          empty={list ? "지역이 없습니다." : "불러오는 중…"} />
        <div className="cap">OTA 는 단말 하나 또는 법정동(말단) 하나씩 보냅니다. 시·구 단위 일괄은 막혀 있습니다(사양서 §24.5).</div>
        <h4 style={{ marginTop: 12 }}>단말 하나 (UUID)</h4>
        <div className="bar2">
          <input value={uuid} placeholder="UUID 24자리" style={{ flex: 1, minWidth: 0 }} onChange={(e) => setUuid(e.target.value)} />
          <button type="button" className="btn" disabled={!pkg || uuid.trim().length !== 24} onClick={sendUuid}>이 단말에 보내기</button>
        </div>
      </Card>

      <div className="col">
        <Card title="진행 규칙" meta={ov ? (ov.window_open ? <span className="c-ok">지금 시작 가능</span> : <span className="c-warn">지금은 시간대 밖 — 대기</span>) : ""}>
          {ov && (
            <div className="cap">
              동시 {ov.parallel}대 · 새로 시작하는 시간 {winText} (서버 설정 → 원격 펌웨어 갱신에서 바꿈) · 지금 대기 {nf(ov.open.waiting)} · 응답 기다림 {nf(ov.open.sending)} · 받는 중 {nf(ov.open.running)}.
              {" "}한 대가 끝나면(성공·되돌림·오류) 다음 단말을 시작합니다. 단말 1대 약 470KB(조각 {nf(ov.chunk)} 글자씩 약 48번). {warnText}
            </div>
          )}
        </Card>

        <Card title="펌웨어 파일" meta={pkgs ? `${pkgs.length}개` : ""}>
          <Upload fwMax={ov?.fw_max ?? 163840} onDone={(m) => (setMsg(m), refresh())} />
          {pkgs && pkgs.length > 0 && (
            <div style={{ maxHeight: 240, overflow: "auto", marginTop: 12 }}>
              <table className="mini">
                <thead><tr><th /><th>파일</th><th>버전</th><th className="n">펌웨어</th><th className="n">pkg</th><th>SHA-256</th><th className="n">성공</th><th>올린 때</th><th /></tr></thead>
                <tbody>
                  {pkgs.map((p) => (
                    <tr key={p.id} className={p.id === pkgId ? "sel" : ""} data-click onClick={() => setPkgId(p.id)}>
                      <td><input type="radio" name="otapkg" checked={p.id === pkgId} onChange={() => setPkgId(p.id)} aria-label={`${p.filename} 고르기`} /></td>
                      <td title={p.note ?? ""}>{p.filename} <small className="muted">#{p.id}</small></td>
                      <td>{p.version ?? "-"}</td>
                      <td className="n">{kb(p.fw_size)}</td>
                      <td className="n">{nf(p.size)}</td>
                      <td className="mono" title={p.sha256}>{p.sha256.slice(0, 12)}…</td>
                      <td className="n">{p.done ? <span className="c-ok">{nf(p.done)}대</span> : <span className="muted">0</span>}</td>
                      <td title={localTime(p.created_at)}>{relTime(p.created_at)} <small className="muted">{p.created_by}</small></td>
                      <td><button type="button" className="btn sm danger" onClick={async (e) => {
                        e.stopPropagation();
                        if (!confirm(`${p.filename}(#${p.id}) 파일을 지울까요? 보낸 기록은 남습니다.`)) return;
                        try { await api.otaDeletePackage(p.id); if (pkgId === p.id) setPkgId(null); refresh(); } catch (x) { setErr(errorText(x)); }
                      }}>지우기</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="보내기" meta={pkg ? `${pkg.filename}${pkg.version ? ` (${pkg.version})` : ""}` : "파일을 고르세요"}>
          {msg && <div className="okl">{msg}</div>}
          {err && <div className="err">{err}</div>}
          {!node ? <div className="cap">왼쪽에서 법정동을 고르면 그 아래 단말이 보입니다. 처음 쓰는 파일은 단말 1대에 먼저 보내 "성공"을 확인하세요.</div>
            : !isLeaf(node) ? <div className="cap">법정동(말단)을 고르세요 — 시·구 단위는 보낼 수 없습니다.</div>
            : (
              <>
                <div className="bar2">
                  <div className="tpath">선택: <b>{pathOf(node)}</b> · 운영 {nf(active.length)}대 (온라인 {nf(active.filter((d) => d.online).length)})</div>
                  <span className="sp" />
                  <button type="button" className="btn pri" disabled={!pkg || active.length === 0}
                    onClick={() => send("node", String(node.r.id), pathOf(node), active.length)}>법정동 전체에 보내기</button>
                </div>
                <div style={{ maxHeight: 320, overflow: "auto", marginTop: 8 }}>
                  <table className="mini">
                    <thead><tr><th>단말</th><th>통신</th><th>펌웨어</th><th>마지막 OTA</th><th /></tr></thead>
                    <tbody>
                      {(devs ?? []).map((d) => (
                        <tr key={d.uuid}>
                          <td data-click onClick={() => onSelect(d.uuid)}>{d.site ?? "-"} <small className="mono muted">{d.uuid.slice(-8)}</small>{d.state !== "ACTIVE" && <small className="muted"> · 운영 아님</small>}</td>
                          <td><OnlineMark on={d.online} /></td>
                          <td className="mono">{d.fw ?? "-"}</td>
                          <td>{d.last_ota ? <><Badge s={d.last_ota.status} /> <small className="muted">#{d.last_ota.job_id} {relTime(d.last_ota.at)}</small></> : <span className="muted">없음</span>}</td>
                          <td><button type="button" className="btn sm" disabled={!pkg || d.state !== "ACTIVE" || (!!d.last_ota && OPEN.includes(d.last_ota.status))}
                            title={d.last_ota && OPEN.includes(d.last_ota.status) ? "이미 OTA 대기·진행 중" : undefined}
                            onClick={() => send("device", d.uuid, d.site ? `${d.site}(${d.uuid.slice(-6)})` : d.uuid, 1)}>이 단말만</button></td>
                        </tr>
                      ))}
                      {devs && devs.length === 0 && <tr><td colSpan={5} className="muted">단말이 없습니다.</td></tr>}
                    </tbody>
                  </table>
                </div>
              </>
            )}
        </Card>
      </div>

      <Card className="full" title="보낸 기록" meta={`${nf(jobTotal)}건 · 열어 둔 기록은 5초마다 갱신`}>
        {job !== null && <JobDetail key={job} id={job} onClose={() => setJob(null)} onSelect={onSelect} onChanged={refresh} />}
        <table className="mini" style={{ marginTop: job !== null ? 12 : 0 }}>
          <thead><tr><th>#</th><th>보낸 때</th><th>대상</th><th>파일</th><th className="n">대수</th><th className="n">성공</th><th className="n">실패·응답 없음</th><th className="n">진행·대기</th><th>끝</th></tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id} className={j.id === job ? "sel" : ""} data-click onClick={() => setJob(j.id)}>
                <td>{j.id}</td>
                <td title={localTime(j.created_at)}>{relTime(j.created_at)} <small className="muted">{j.created_by}</small></td>
                <td>{j.scope_label}</td>
                <td>{j.filename}{j.version ? ` (${j.version})` : ""}</td>
                <td className="n">{nf(j.total)}</td>
                <td className="n c-ok">{nf(j.counts.DONE ?? 0)}</td>
                <td className="n">{sum(j.counts, FAIL) ? <span className="c-alarm">{nf(sum(j.counts, FAIL))}</span> : 0}</td>
                <td className="n">{nf(sum(j.counts, OPEN))}</td>
                <td>{j.finished_at ? localTime(j.finished_at) : j.cancelled_at ? "취소됨" : <span className="c-blue">진행 중</span>}</td>
              </tr>
            ))}
            {jobs.length === 0 && <tr><td colSpan={9} className="muted">보낸 기록이 없습니다.</td></tr>}
          </tbody>
        </table>
        <Pager page={page} pages={pageCount(jobTotal, 20)} total={jobTotal} size={20} onPage={setPage} />
      </Card>
    </div>
  );
}
