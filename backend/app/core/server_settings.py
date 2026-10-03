"""서버 설정 — 운영 중 화면에서 바꾸는 값(문제점 14번, ADR-012).

`.env` 는 배포 담당이 서버에서 고치고 재시작해야 한다. 운영자가 바꿔야 하는 값은 여기 표(ITEMS)에 올리고
DB(`server_setting`)에 둔다. 항목을 늘릴 때는 ITEMS 에 한 줄 + 쓰는 곳에서 `runtime.get(key)` — 화면·API·검사는
표를 보고 저절로 따라온다. 최고관리자만 바꾼다. 모든 단말에 똑같이 적용된다.

값은 프로세스 메모리(`runtime`)에 들고 있다 — 기동 때 DB 에서 읽고, 저장할 때 같이 바꾼다(백엔드는 한 프로세스).

저장은 전부 정수다(`server_setting.value integer`). 소수가 필요한 항목은 `scale`(표시값 × scale),
날짜는 `kind="date"` 로 YYYYMMDD 정수(0 = 없음). 화면은 kind·scale 을 보고 입력칸을 고른다.

묶음(2026-10-03, 문제점 27·29번): 원격 명령 / 기록 보관 기간 / 발전·사용 통계.
"""

from __future__ import annotations

import datetime as dt
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
    #: 저장값 = 표시값 × scale (예: 10 이면 소수 1자리)
    scale: int = 1
    #: "int" | "date"(YYYYMMDD 정수, 0 = 없음)
    kind: str = "int"


#: (id, 화면 제목). 화면은 이 순서로 묶어 보여 준다.
GROUPS: tuple[tuple[str, str], ...] = (
    ("command", "원격 명령 (점등·소등·밝기)"),
    ("retention", "기록 보관 기간"),
    ("energy", "발전·사용 통계"),
)

ITEMS: tuple[Item, ...] = (
    Item("command_wait_sec", "command", "응답 기다리는 시간",
         "명령을 보낸 뒤 단말의 응답을 기다리는 시간. 이 시간 동안 응답이 없으면 다시 보낸다.",
         "초", 10, 180, 30),
    Item("command_attempts", "command", "보내는 횟수",
         "처음 보낸 것을 포함한 횟수. 마지막까지 응답이 없으면 '무응답(실패)'으로 끝난다.",
         "회", 1, 3, 2),
    # ── 보관 기간(개월). 지난 것은 매일 01:00 에 지운다. 지워도 하루 요약·누적·현재 값은 남는다.
    Item("telemetry_months", "retention", "10분 보고 원문",
         "단말이 10분마다 보내는 보고 원문. 달 단위로 지운다. "
         "하루 요약(발전·사용·점등 시간)은 영구히 남는다.",
         "개월", 1, 13, 3),
    Item("event_months", "retention", "단말 이벤트",
         "접속·두절·재부팅·상태 변경 기록.", "개월", 1, 24, 6),
    Item("alarm_months", "retention", "알람 이력",
         "해제된 알람. 열린 알람은 지우지 않는다.", "개월", 3, 36, 12),
    Item("command_months", "retention", "원격 명령·응답",
         "보낸 명령과 단말별 응답 기록.", "개월", 1, 24, 6),
    Item("settings_history_months", "retention", "단말 설정 변경 이력",
         "누가 언제 무엇을 바꿨는지. 현재 값은 영구히 남는다.", "개월", 3, 36, 12),
    Item("deploy_months", "retention", "그룹 스케줄 보낸 기록",
         "보낸 기록과 단말별 결과. 단말별 적용 상태는 영구히 남는다.", "개월", 1, 24, 6),
    # ── 발전·사용 통계
    Item("ghg_g_per_kwh", "energy", "온실가스 배출계수",
         "감축량 = 발전량(kWh) × 이 값. 국가 전력 배출계수(기본 478.1 gCO2eq/kWh). "
         "바꾸면 누적 감축이 다시 계산된다.",
         "g/kWh", 1000, 20000, 4781, scale=10),
    Item("stats_since", "energy", "통계 시작일",
         "이 날부터의 기록만 그래프·누적에 넣는다. 비우면 전체 기간. "
         "'통계 다시 시작'을 누르면 오늘로 바뀐다.",
         "", 0, 20991231, 0, kind="date"),
)

BY_KEY: dict[str, Item] = {i.key: i for i in ITEMS}


def _date_ok(v: int) -> bool:
    if v == 0:
        return True
    try:
        dt.date(v // 10000, v // 100 % 100, v % 100)
    except ValueError:
        return False
    return True


def validate(values: dict[str, object]) -> tuple[dict[str, int], dict[str, str]]:
    """(통과한 값, {key: 이유}). 모르는 key·정수 아님·범위 밖·날짜 아님은 이유에 담는다.
    값은 저장값(scale 을 곱한 정수)."""
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
            if item.kind == "date":
                errors[key] = "날짜(YYYYMMDD) 또는 0"
            elif item.scale > 1:
                errors[key] = f"{item.min / item.scale:g}~{item.max / item.scale:g}{item.unit}"
            else:
                errors[key] = f"{item.min}~{item.max}{item.unit}"
            continue
        if item.kind == "date" and not _date_ok(raw):
            errors[key] = "날짜(YYYYMMDD) 또는 0"
            continue
        clean[key] = raw
    return clean, errors


def date_of(v: int) -> dt.date | None:
    """YYYYMMDD 정수 → date. 0·잘못된 값은 None."""
    if not v or not _date_ok(v):
        return None
    return dt.date(v // 10000, v // 100 % 100, v % 100)


def int_of(d: dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


class Runtime:
    """지금 적용 중인 값. DB 에 없는 항목은 기본값."""

    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    def apply(self, stored: dict[str, object]) -> None:
        """DB 에서 읽은 값으로 통째로 바꾼다. 범위 밖 옛 값·모르는 key 는 버린다(기본값으로)."""
        clean, _ = validate(stored)
        self._values = clean

    def update(self, values: dict[str, int]) -> None:
        self._values.update(values)

    def get(self, key: str) -> int:
        return self._values.get(key, BY_KEY[key].default)

    # ── 원격 명령 ───────────────────────────────────────────────────
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

    # ── 보관 기간 ───────────────────────────────────────────────────
    @property
    def telemetry_retention_months(self) -> int:
        return self.get("telemetry_months")

    def retention_days(self, key: str) -> int:
        """개월 항목 → 일수(30일/개월)."""
        return self.get(key) * 30

    # ── 발전·사용 통계 ───────────────────────────────────────────────
    @property
    def ghg_kg_per_kwh(self) -> float:
        return self.get("ghg_g_per_kwh") / 10 / 1000

    @property
    def stats_since(self) -> dt.date | None:
        return date_of(self.get("stats_since"))


runtime = Runtime()
