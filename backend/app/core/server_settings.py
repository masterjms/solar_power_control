"""서버 설정 — 운영 중 화면에서 바꾸는 값(문제점 14번, ADR-012).

`.env` 는 배포 담당이 서버에서 고치고 재시작해야 한다. 운영자가 바꿔야 하는 값은 여기 표(ITEMS)에 올리고
DB(`server_setting`)에 둔다. 항목을 늘릴 때는 ITEMS 에 한 줄 + 쓰는 곳에서 `runtime.get(key)` — 화면·API·검사는
표를 보고 저절로 따라온다. 최고관리자만 바꾼다. 모든 단말에 똑같이 적용된다.

값은 프로세스 메모리(`runtime`)에 들고 있다 — 기동 때 DB 에서 읽고, 저장할 때 같이 바꾼다(백엔드는 한 프로세스).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    key: str
    group: str
    label: str
    help: str
    unit: str
    min: int
    max: int
    default: int


#: (id, 화면 제목). 화면은 이 순서로 묶어 보여 준다.
GROUPS: tuple[tuple[str, str], ...] = (
    ("command", "원격 명령 (점등·소등·밝기)"),
)

ITEMS: tuple[Item, ...] = (
    Item("command_wait_sec", "command", "응답 기다리는 시간",
         "명령을 보낸 뒤 단말의 응답을 기다리는 시간. 이 시간 동안 응답이 없으면 다시 보낸다.",
         "초", 10, 180, 30),
    Item("command_attempts", "command", "보내는 횟수",
         "처음 보낸 것을 포함한 횟수. 마지막까지 응답이 없으면 '무응답(실패)'으로 끝난다.",
         "회", 1, 3, 2),
)

BY_KEY: dict[str, Item] = {i.key: i for i in ITEMS}


def validate(values: dict[str, object]) -> tuple[dict[str, int], dict[str, str]]:
    """(통과한 값, {key: 이유}). 모르는 key·정수 아님·범위 밖은 이유에 담는다."""
    clean: dict[str, int] = {}
    errors: dict[str, str] = {}
    for key, raw in values.items():
        item = BY_KEY.get(key)
        if item is None:
            errors[key] = "없는 항목"
            continue
        if isinstance(raw, bool) or not isinstance(raw, int):
            errors[key] = "정수여야 한다"
            continue
        if not item.min <= raw <= item.max:
            errors[key] = f"{item.min}~{item.max}{item.unit}"
            continue
        clean[key] = raw
    return clean, errors


class Runtime:
    """지금 적용 중인 값. DB 에 없는 항목은 기본값."""

    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    def apply(self, stored: dict[str, object]) -> None:
        """DB 에서 읽은 값으로 통째로 바꾼다. 범위를 벗어난 옛 값·모르는 key 는 버린다(기본값으로)."""
        clean, _ = validate(stored)
        self._values = clean

    def update(self, values: dict[str, int]) -> None:
        self._values.update(values)

    def get(self, key: str) -> int:
        return self._values.get(key, BY_KEY[key].default)

    # ── 자주 쓰는 것 ────────────────────────────────────────────────
    @property
    def command_wait_sec(self) -> int:
        return self.get("command_wait_sec")

    @property
    def command_attempts(self) -> int:
        return self.get("command_attempts")

    @property
    def command_timeout_sec(self) -> int:
        """명령을 실패로 닫는 시각 = 기다리는 시간 × 보내는 횟수(기본 30 × 2 = 60초)."""
        return self.command_wait_sec * self.command_attempts


runtime = Runtime()
