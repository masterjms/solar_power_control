// 대시보드 "최근 활동"(문제점 30·34번). GET /api/activity — 알람·단말 상태·원격 조작·관리(최고관리자) 기록을 최신순으로.
// 검색(시설명·주소·지역), 분류, 목록 개수 20/50/100(기본 20, 문제점 35번), 쪽 넘기기. 열 순서: 시설명 · 지역 · 시각 · 분류 · 내용.
import { useEffect, useState } from "react";
import { ActivityItem, ActivityPage, api, errorText } from "./api";
import { localTime, relTime } from "./format";
import { Card, DEFAULT_PAGE_SIZE, PageSize, Pager, nf } from "./ui";

const REFRESH_MS = 10_000;
const CAT_LABEL: Record<string, string> = { alarm: "알람", device: "단말", control: "조작", admin: "관리" };
const CAT_CLS: Record<string, string> = { alarm: "b-alarm", device: "b-blue", control: "b-ok", admin: "b-off" };

export default function ActivityCard({ tick, onSelect, readOnly = false }: { tick: number; onSelect: (uuid: string) => void; readOnly?: boolean }) {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [cat, setCat] = useState("");
  const [size, setSize] = useState(DEFAULT_PAGE_SIZE);
  const [page, setPage] = useState(1);
  const [res, setRes] = useState<ActivityPage | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => api.activity({ page, size, cat: cat || undefined, q: query || undefined })
      .then((r) => alive && (setRes(r), setErr(null))).catch((e) => alive && setErr(errorText(e)));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => { alive = false; clearInterval(id); };
  }, [page, size, cat, query, tick]);

  const rows: ActivityItem[] = res?.items ?? [];
  // 쪽 넘기기는 최근 max_rows 건까지(서버와 같은 한도) — 그 전은 검색·분류로.
  const shown = Math.min(res?.total ?? 0, res?.max_rows ?? Infinity);
  const pages = Math.max(1, Math.ceil(shown / size));

  return (
    <Card title="최근 활동" className="full" meta={res ? `${nf(res.total)}건 · 10초마다` : ""}>
      <div className="bar2">
        <input className="inp" type="search" value={q} placeholder="시설명·주소·지역 검색" aria-label="최근 활동 검색" style={{ maxWidth: 260 }}
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && (setQuery(q.trim()), setPage(1))} />
        <button type="button" className="btn" onClick={() => (setQuery(q.trim()), setPage(1))}>검색</button>
        <span className="chips">
          {["", ...(res?.cats ?? ["alarm", "device", "control"])].map((c) => (
            <button key={c || "all"} type="button" className={`chip ${cat === c ? "on" : ""}`} aria-pressed={cat === c}
              onClick={() => (setCat(c), setPage(1))}>{c ? CAT_LABEL[c] : "전체"}</button>
          ))}
        </span>
        <span className="sp" />
        <PageSize size={size} onChange={(n) => (setSize(n), setPage(1))} />
      </div>
      {err && <div className="err">{err}</div>}
      <div className="tw">
        <table className="list">
          <thead><tr><th>시설명</th><th>지역</th><th>시각</th><th>분류</th><th>내용</th><th>누가</th></tr></thead>
          <tbody>
            {rows.map((r, i) => {
              const click = !readOnly && r.uuid ? () => onSelect(r.uuid!) : undefined;
              return (
                <tr key={`${r.at}-${i}`} data-click={click ? true : undefined} onClick={click}>
                  <td>{r.site ?? (r.uuid ? <span className="mono muted" title={r.uuid}>…{r.uuid.slice(-6)}</span> : <span className="muted">-</span>)}</td>
                  <td title={r.region ?? ""}>{r.region ? r.region.split(">").map((x) => x.trim()).slice(-2).join(" > ") : ""}</td>
                  <td title={localTime(r.at)}>{relTime(r.at)}</td>
                  <td><span className={`badge ${r.cat === "alarm" && r.severity === "info" ? "b-off" : CAT_CLS[r.cat]}`}>{CAT_LABEL[r.cat]}</span></td>
                  <td>{r.text}</td>
                  <td className="muted">{r.by ?? ""}</td>
                </tr>
              );
            })}
            {rows.length === 0 && <tr><td colSpan={6} className="empty">{res ? "기록이 없습니다." : "불러오는 중…"}</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager page={page} pages={pages} total={res?.total ?? 0} size={size} onPage={setPage} />
      {res && res.total > res.max_rows && page === pages && (
        <div className="muted">최근 {nf(res.max_rows)}건까지 넘겨 볼 수 있습니다 — 그 전 기록은 검색·분류로 좁혀 보세요.</div>
      )}
    </Card>
  );
}
