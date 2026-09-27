// #group 그룹 제어(S-20) — 왼쪽 탐색기 트리("전체" 루트 포함), 오른쪽 대상 요약 + 명령 폼,
// 아래에 결과(응답 집계·개별 재시도)와 명령 이력. 목업 v15 의 #gdrawer 를 화면 한 장으로 폈다.
import { ReactNode, useState } from "react";
import { CommandTargetRef, DeviceCounts, Role } from "./api";
import { CommandForm, CommandHistory, CommandResult } from "./Command";
import { RegionTree, TreeSel, isLeaf, leavesOf, pathOf, useRegions } from "./Tree";
import { Card, nf } from "./ui";

interface Props {
  role: Role | null;
  counts: DeviceCounts | null;
  tick: number;
  onSelect: (uuid: string) => void;
}

export default function GroupControl({ role, counts, tick, onSelect }: Props) {
  const { tree, list, error } = useRegions(tick);
  const [sel, setSel] = useState<TreeSel>(null);
  const [filter, setFilter] = useState("");
  const [seq, setSeq] = useState<number | null>(null);
  const [onlySel, setOnlySel] = useState(false);
  const isSuper = role === "super_admin";
  const node = typeof sel === "number" ? tree.byId.get(sel) ?? null : null;

  let target: CommandTargetRef | null = null;
  let label = "대상 없음";
  let summary: ReactNode = <div className="cap">왼쪽 트리에서 대상을 고른다. 상위를 고르면 하위 전체가 대상이다.</div>;

  if (sel === "all") {
    target = { kind: "all", id: null };
    label = "전체 단말";
    const total = counts ? counts.PENDING + counts.ACTIVE + counts.SUSPENDED + counts.REJECTED + counts.RETIRED : null;
    summary = (
      <>
        <div className="tpath">선택: <b>전체</b> (지역 배정과 무관하게 모든 단말)</div>
        <div className="grid2">
          <div className="met"><div className="l">운영(ACTIVE)</div><div className="v">{nf(counts?.ACTIVE)}대</div><div className="h">등록 단말 {nf(total)}대</div></div>
          <div className="met"><div className="l">발행</div><div className="v mono">iotlight/all/cmd</div><div className="h">1회</div></div>
        </div>
        {!isSuper && <div className="err">전체 명령은 최고관리자만 보낼 수 있다.</div>}
      </>
    );
  } else if (node) {
    target = { kind: "node", id: String(node.r.id) };
    label = pathOf(node);
    const leaf = isLeaf(node);
    const leaves = leavesOf(node);
    summary = (
      <>
        <div className="tpath">선택: <b>{pathOf(node)}</b>{leaf && node.r.bjd_code ? ` (${node.r.bjd_code})` : ""}</div>
        <div className="grid2">
          <div className="met"><div className="l">단말 (하위 전체)</div><div className="v">{nf(node.r.device_count)}대</div><div className="h">운영(ACTIVE) {nf(node.r.active_count)}대 — 이들에게만 보낸다</div></div>
          <div className="met"><div className="l">발행</div>
            <div className={leaf ? "v mono" : "v"}>{leaf ? `iotlight/group/${node.r.grp ?? "?"}/cmd` : `법정동 topic ${nf(leaves.length)}개`}</div>
            <div className="h">{leaf ? "그룹 topic 1회" : "하위 법정동 topic 마다 1회, seq 는 하나"}</div></div>
        </div>
      </>
    );
  }

  const blocked = sel === "all" && !isSuper ? "전체 명령은 최고관리자만" : null;

  return (
    <div className="explorer">
      <Card title="대상" meta={list ? `법정동 ${nf(list.filter((r) => r.level === "dong").length)}개` : ""}>
        <input type="search" placeholder="이름 · 법정동코드로 찾기" aria-label="트리 필터" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {error && <div className="err">{error}</div>}
        <RegionTree
          tree={tree}
          selected={sel}
          onSelect={(s) => (setSel(s), setOnlySel(false))}
          filter={filter}
          className="tall"
          root={{ label: "전체", selectable: isSuper, note: isSuper ? "전체 단말 (iotlight/all/cmd)" : "전체 명령은 최고관리자만" }}
          empty={list ? "지역이 없습니다. '지역(법정동)'에서 추가한다." : "불러오는 중…"}
        />
        <div className="cap">개별 &gt; 그룹 &gt; 전체 순으로 우선. 유지시간이 지나면 스케줄로 복귀한다. 개별 명령은 단말 상세(드로어)에서.</div>
      </Card>

      <div className="col">
        <Card title="선택한 대상" meta={role ? (isSuper ? "최고관리자" : "관리자") : ""}>{summary}</Card>
        <Card title="명령" meta="§3.10.7 COMMAND">
          <CommandForm target={target} targetLabel={label} blocked={blocked} onSent={(c) => setSeq(c.seq)} />
        </Card>
      </div>

      {seq !== null && (
        <section className="card full">
          <div className="cb" style={{ paddingTop: 16 }}>
            <CommandResult seq={seq} onClose={() => setSeq(null)} onDevice={onSelect} />
          </div>
        </section>
      )}

      <Card
        title="명령 이력"
        className="full"
        meta={
          <label className="chk2">
            <input type="checkbox" checked={onlySel} disabled={!node} onChange={(e) => setOnlySel(e.target.checked)} />
            선택한 지역만
          </label>
        }
      >
        <CommandHistory tick={tick} nodeId={onlySel && node ? node.r.id : undefined} selected={seq} onOpen={setSeq} />
      </Card>
    </div>
  );
}
