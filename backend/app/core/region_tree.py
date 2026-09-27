"""법정동 트리 순수 도우미 — 경로 이름, 하위 말단, 그룹 id (ADR-005, 사양서 §3.10.4).

트리는 시도 수십 + 시군구 수백 + 법정동 수천 규모라 요청마다 통째로 메모리에 올려도 싸다.
그래서 경로·하위 말단 계산은 DB 재귀 대신 여기서 한다(단말 수 집계만 SQL — region 서비스).
DB 를 모르는 순수 함수라 시험이 직접 부른다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.constants import GROUP_SUFFIX, RegionLevel

PATH_SEP = " > "


@dataclass(frozen=True)
class Node:
    id: int
    parent_id: int | None
    level: str
    name: str
    bjd_code: str | None = None
    lat: float | None = None
    lon: float | None = None

    @property
    def is_leaf(self) -> bool:
        """말단 = 법정동(그룹). 상위 노드는 하위 말단이 없어도 말단이 아니다."""
        return self.level == RegionLevel.DONG.value

    @property
    def grp(self) -> str | None:
        return group_id(self.bjd_code) if self.bjd_code else None


def group_id(bjd_code: str) -> str:
    """group_id = 법정동코드 10자리 + 확장 "00" = 12자리(§3.10.4). 단말은 12자리 숫자인지만 본다."""
    return f"{bjd_code}{GROUP_SUFFIX}"


class Tree:
    def __init__(self, nodes: Iterable[Node]) -> None:
        self.nodes: dict[int, Node] = {n.id: n for n in nodes}
        self.children: dict[int | None, list[int]] = {}
        for n in self.nodes.values():
            self.children.setdefault(n.parent_id, []).append(n.id)
        for ids in self.children.values():
            ids.sort()

    def get(self, node_id: int | None) -> Node | None:
        return None if node_id is None else self.nodes.get(node_id)

    def path(self, node_id: int) -> list[Node]:
        """루트 → 그 노드. 순환(있어선 안 되지만)이면 멈춘다."""
        out: list[Node] = []
        seen: set[int] = set()
        cur = self.nodes.get(node_id)
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            out.append(cur)
            cur = self.nodes.get(cur.parent_id) if cur.parent_id is not None else None
        out.reverse()
        return out

    def path_name(self, node_id: int | None) -> str | None:
        """예 "경기도 > 안양시 만안구 > 안양동". 없는 노드면 None."""
        if node_id is None or node_id not in self.nodes:
            return None
        return PATH_SEP.join(n.name for n in self.path(node_id))

    def subtree_ids(self, node_id: int) -> list[int]:
        """자신 포함 하위 전부(너비 우선)."""
        if node_id not in self.nodes:
            return []
        out: list[int] = []
        queue = [node_id]
        seen: set[int] = set()
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            out.append(cur)
            queue.extend(self.children.get(cur, []))
        return out

    def leaves_under(self, node_id: int) -> list[Node]:
        """자신 포함 하위 말단(법정동). 말단이면 자기 하나. bjd_code 순으로 — topic 순서 고정."""
        leaves = [self.nodes[i] for i in self.subtree_ids(node_id) if self.nodes[i].is_leaf]
        return sorted(leaves, key=lambda n: n.bjd_code or "")

    def coords_for(self, node_id: int | None) -> tuple[float, float] | None:
        """"오늘 밤" 계산용 대표 좌표: 자기 좌표 → 하위 말단 중 첫 좌표. 없으면 None."""
        node = self.get(node_id)
        if node is None:
            return None
        if node.lat is not None and node.lon is not None:
            return node.lat, node.lon
        for leaf in self.leaves_under(node.id):
            if leaf.lat is not None and leaf.lon is not None:
                return leaf.lat, leaf.lon
        return None
