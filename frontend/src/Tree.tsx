// 법정동 트리 — VS Code 탐색기처럼 왼쪽에 접고 펴는 트리(▸/▾, 들여쓰기, 노드마다 단말 수).
// GET /api/regions 평면 목록을 화면에서 조립한다(docs/05 5차). 목업 v15 의 .tree/.tn 모양.
import { KeyboardEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { api, Region, errorText } from "./api";
import { nf } from "./ui";

export interface TreeNode {
  r: Region;
  depth: number;
  parent: TreeNode | null;
  children: TreeNode[];
}

export interface RegionTreeData {
  roots: TreeNode[];
  byId: Map<number, TreeNode>;
  total: number; // 최상위 노드 device_count 합 = 트리에 배정된 단말 수
  totalActive: number;
}

const LEVEL_ORDER: Record<string, number> = { sido: 0, sigungu: 1, dong: 2 };

/** 평면 목록 → 트리. 부모를 못 찾는 노드는 최상위에 둔다. 이름순. */
export function buildTree(list: Region[]): RegionTreeData {
  const byId = new Map<number, TreeNode>();
  list.forEach((r) => byId.set(r.id, { r, depth: 0, parent: null, children: [] }));
  const roots: TreeNode[] = [];
  byId.forEach((n) => {
    const p = n.r.parent_id !== null ? byId.get(n.r.parent_id) : undefined;
    if (p && p !== n) {
      n.parent = p;
      p.children.push(n);
    } else roots.push(n);
  });
  const sort = (a: TreeNode[]) => {
    a.sort((x, y) => (LEVEL_ORDER[x.r.level] ?? 9) - (LEVEL_ORDER[y.r.level] ?? 9) || x.r.name.localeCompare(y.r.name, "ko"));
    a.forEach((c) => sort(c.children));
  };
  sort(roots);
  const setDepth = (n: TreeNode, d: number) => {
    n.depth = d;
    n.children.forEach((c) => setDepth(c, d + 1));
  };
  roots.forEach((n) => setDepth(n, 0));
  return {
    roots,
    byId,
    total: roots.reduce((a, n) => a + n.r.device_count, 0),
    totalActive: roots.reduce((a, n) => a + n.r.active_count, 0),
  };
}

/** 말단 = 법정동(bjd_code 를 가진 노드). level 이 dong 이거나 bjd_code 가 있으면 말단으로 본다. */
export const isLeaf = (n: TreeNode) => n.r.level === "dong" || (!!n.r.bjd_code && n.children.length === 0);

export function pathOf(n: TreeNode): string {
  const parts: string[] = [];
  for (let x: TreeNode | null = n; x; x = x.parent) parts.unshift(x.r.name);
  return parts.join(" > ");
}

export function leavesOf(n: TreeNode): TreeNode[] {
  if (isLeaf(n)) return [n];
  return n.children.flatMap(leavesOf);
}

/** 트리 목록 읽기. tick 이 바뀌면 다시 읽는다. */
export function useRegions(tick = 0) {
  const [list, setList] = useState<Region[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  const reload = useCallback(() => setN((x) => x + 1), []);
  useEffect(() => {
    let alive = true;
    api
      .listRegions()
      .then((r) => alive && (setList(r), setError(null)))
      .catch((e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [tick, n]);
  const tree = useMemo(() => buildTree(list ?? []), [list]);
  return { list, tree, error, reload };
}

/** 선택값: 노드 id, 또는 "all"(전체 루트), 또는 없음. */
export type TreeSel = number | "all" | null;

interface Props {
  tree: RegionTreeData;
  selected: TreeSel;
  onSelect: (s: TreeSel) => void;
  /** 이 노드를 고를 수 있나(기본: 전부). 못 고르는 노드는 흐리게, 펼치기는 된다. */
  canSelect?: (n: TreeNode) => boolean;
  /** 맨 위에 "전체" 루트를 둔다(그룹 제어). */
  root?: { label: string; selectable: boolean; note?: string };
  filter?: string;
  className?: string;
  empty?: string;
  /** 노드 이름 뒤에 붙일 표시(스케줄 배정 프로필 등). */
  badge?: (n: TreeNode) => ReactNode;
}

/** 탐색기식 트리. 행 클릭 = 선택(+ 접힌 상위면 펼침), ▸/▾ = 펼치기만. 키보드 ↑↓ ←→ Enter. */
export function RegionTree({ tree, selected, onSelect, canSelect, root, filter, className, empty, badge }: Props) {
  const [open, setOpen] = useState<Set<number>>(() => new Set());
  const [rootOpen, setRootOpen] = useState(true);
  const q = (filter ?? "").trim().toLowerCase();

  // 처음 읽었을 때 최상위는 펼쳐 둔다. 선택된 노드의 조상도 펼친다.
  useEffect(() => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (prev.size === 0) tree.roots.forEach((n) => next.add(n.r.id));
      if (typeof selected === "number") {
        for (let x = tree.byId.get(selected)?.parent ?? null; x; x = x.parent) next.add(x.r.id);
      }
      return next.size === prev.size ? prev : next;
    });
  }, [tree, selected]);

  // 필터: 이름·코드가 맞는 노드와 그 조상만. 필터 중엔 전부 펼친 것처럼 보인다.
  const visibleIds = useMemo(() => {
    if (!q) return null;
    const keep = new Set<number>();
    tree.byId.forEach((n) => {
      if (n.r.name.toLowerCase().includes(q) || (n.r.bjd_code ?? "").includes(q)) {
        for (let x: TreeNode | null = n; x; x = x.parent) keep.add(x.r.id);
      }
    });
    return keep;
  }, [q, tree]);

  const toggle = (id: number) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  // 화면에 보이는 행 순서(키보드 이동용)
  const rows: { key: string; node: TreeNode | null; depth: number }[] = [];
  const baseDepth = root ? 1 : 0;
  if (root) rows.push({ key: "all", node: null, depth: 0 });
  const walk = (ns: TreeNode[]) =>
    ns.forEach((n) => {
      if (visibleIds && !visibleIds.has(n.r.id)) return;
      rows.push({ key: String(n.r.id), node: n, depth: n.depth + baseDepth });
      const isOpen = visibleIds ? true : open.has(n.r.id);
      if (isOpen && n.children.length) walk(n.children);
    });
  if (!root || rootOpen || visibleIds) walk(tree.roots);

  const selKey = selected === null ? null : String(selected);
  const selectable = (n: TreeNode | null) => (n === null ? !!root?.selectable : canSelect ? canSelect(n) : true);

  function choose(n: TreeNode | null) {
    if (!selectable(n)) {
      if (n && n.children.length) toggle(n.r.id);
      return;
    }
    if (n === null) setRootOpen(true);
    else if (n.children.length && !open.has(n.r.id)) toggle(n.r.id);
    onSelect(n === null ? "all" : n.r.id);
  }

  function onKey(e: KeyboardEvent<HTMLDivElement>, i: number) {
    const row = rows[i];
    const focus = (j: number) => {
      const el = e.currentTarget.parentElement?.querySelectorAll<HTMLElement>(".tn")[j];
      el?.focus();
    };
    if (e.key === "ArrowDown") (e.preventDefault(), focus(Math.min(rows.length - 1, i + 1)));
    else if (e.key === "ArrowUp") (e.preventDefault(), focus(Math.max(0, i - 1)));
    else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (row.node === null) setRootOpen(true);
      else if (row.node.children.length && !open.has(row.node.r.id)) toggle(row.node.r.id);
      else focus(Math.min(rows.length - 1, i + 1));
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (row.node === null) setRootOpen(false);
      else if (row.node.children.length && open.has(row.node.r.id)) toggle(row.node.r.id);
      else {
        const pk = row.node.parent ? String(row.node.parent.r.id) : root ? "all" : null;
        const j = rows.findIndex((r) => r.key === pk);
        if (j >= 0) focus(j);
      }
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      choose(row.node);
    }
  }

  return (
    <div className={`tree ${className ?? ""}`.trim()} role="tree">
      {rows.map((row, i) => {
        const n = row.node;
        const hasKids = n ? n.children.length > 0 : tree.roots.length > 0;
        const isOpen = n ? (visibleIds ? true : open.has(n.r.id)) : rootOpen || !!visibleIds;
        const can = selectable(n);
        const leaf = n ? isLeaf(n) : false;
        const cnt = n ? n.r.device_count : tree.total;
        const act = n ? n.r.active_count : tree.totalActive;
        return (
          <div
            key={row.key}
            className={`tn ${selKey === row.key ? "on" : ""} ${can ? "" : "dis"}`.trim()}
            style={{ paddingLeft: 6 + row.depth * 16 }}
            role="treeitem"
            aria-selected={selKey === row.key}
            aria-expanded={hasKids ? isOpen : undefined}
            aria-disabled={!can || undefined}
            tabIndex={0}
            title={n ? `${pathOf(n)}${can ? "" : " (고를 수 없음)"}` : root?.note}
            onClick={() => choose(n)}
            onDoubleClick={() => (n ? n.children.length && toggle(n.r.id) : setRootOpen((o) => !o))}
            onKeyDown={(e) => onKey(e, i)}
          >
            <button
              type="button"
              className="tc"
              tabIndex={-1}
              disabled={!hasKids}
              aria-label={isOpen ? "접기" : "펼치기"}
              onClick={(e) => {
                e.stopPropagation();
                if (n) toggle(n.r.id);
                else setRootOpen((o) => !o);
              }}
            >
              {hasKids ? (isOpen ? "▾" : "▸") : ""}
            </button>
            <span className="tnm">
              {n ? n.r.name : root?.label}
              {leaf && n?.r.bjd_code && <small>{n.r.bjd_code}</small>}
              {n && badge?.(n)}
            </span>
            <span className="tct" title={`단말 ${cnt}대 · 운영 ${act}대`}>
              {nf(cnt)}대{cnt !== act && <em>운영 {nf(act)}</em>}
            </span>
          </div>
        );
      })}
      {tree.roots.length === 0 && <div className="tempty">{empty ?? "지역이 없습니다."}</div>}
    </div>
  );
}
