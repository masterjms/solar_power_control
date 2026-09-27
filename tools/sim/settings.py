"""단말 운전 설정(S-23) 순수 부분 — 항목 정의·지문 `sh`·표 CRC·SETTINGS_SET 검사 (펌웨어 2026-09-27-7 모델).

항목 정의는 `docs/spec/settings/ui_items.json` 을 **실행 때** 읽는다(키·순서·범위·기본값의 유일한 기준, ADR-007).
표 계산은 `docs/spec/ref/suntable.py` 를 경로로 import 한다(서버·단말·PC 도구와 비트 단위로 같은 정수 계산).
백엔드 코드는 import 하지 않는다(영역 분리) — 규칙 해석은 여기서 사양 문장대로 다시 구현하고 docs/06 에 적는다.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import zlib
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_ITEMS_PATH = REPO_ROOT / "docs" / "spec" / "settings" / "ui_items.json"
SUNTABLE_PATH = REPO_ROOT / "docs" / "spec" / "ref" / "suntable.py"

#: UI 명세 8.6 — 기본값 25개의 지문. 서울(37566500, 126978000, 0, 0) 표 CRC. 단위 시험으로 고정한다.
DEFAULT_SH = "38AF0DBD"
SEOUL_TBL_CRC = "69C1DF86"
#: 8.3 `tbl.src` — 0 펌웨어 기본 표(부산) / 1 PC 도구 / 2 서버.
SRC_FIRMWARE, SRC_PC_TOOL, SRC_SERVER = 0, 1, 2
#: 펌웨어 기본 표 = 부산(8.3). 좌표는 사양에 없어 부산시청(35.1796, 129.0756)으로 둔다(가정, docs/06 §5 C1).
DEFAULT_TBL_COND: dict[str, Any] = {"region": "부산", "lat_e6": 35179600, "lon_e6": 129075600, "on": 0, "off": 0}
TBL_KEYS: tuple[str, ...] = ("region", "lat_e6", "lon_e6", "on", "off")
#: 8.4 RANGE — 표 조건 좌표 ±90/±180 도(x1e6), 보정 ±180분. `region` 은 UTF-8 47바이트(ui_items year_table).
TBL_LAT_MAX, TBL_LON_MAX, TBL_CORR_MAX = 90_000_000, 180_000_000, 180
REGION_MAX_BYTES = 47
#: 단말 수신 줄 한계(8.4 "단말 수신 줄 1,024B 가 한계"). 서버 발행 상한 900B 는 이보다 작다(ADR-007).
RX_LINE_MAX = 1024
SERVER_PAYLOAD_MAX = 900
#: SETTINGS_ACK `result` 값(8.4).
SETTINGS_RESULTS: tuple[str, ...] = ("OK", "RANGE", "RULE", "CRC", "BAD", "STATE", "FLASH")
MINUTES_PER_DAY = 1440


@lru_cache(maxsize=1)
def ui_items() -> dict[str, Any]:
    return json.loads(UI_ITEMS_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def items() -> tuple[dict[str, Any], ...]:
    """25개 항목, `ui_items.json` 순서 그대로(지문 계산 순서)."""
    return tuple(i for g in ui_items()["groups"] for i in g["items"])


def keys() -> tuple[str, ...]:
    return tuple(i["key"] for i in items())


def defaults() -> dict[str, int]:
    return {i["key"]: int(i["default"]) for i in items()}


@lru_cache(maxsize=1)
def suntable() -> ModuleType:
    """`docs/spec/ref/suntable.py` 를 경로로 import(패키지가 아니다)."""
    spec = importlib.util.spec_from_file_location("spec_suntable", SUNTABLE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def settings_sh(values: dict[str, int]) -> str:
    """8.6 지문: 25개를 ui_items 순서로 `<i`(32비트 LE) 로 이어 붙인 CRC-32, 대문자 8자리."""
    blob = b"".join(struct.pack("<i", int(values[k])) for k in keys())
    return "%08X" % (zlib.crc32(blob) & 0xFFFFFFFF)


@lru_cache(maxsize=256)
def table_crc(lat_e6: int, lon_e6: int, on: int, off: int) -> str:
    """8.3 표 CRC = `suntable.table_crc32(build_table(lat_e6, lon_e6, on, off))`, 대문자 8자리."""
    st = suntable()
    return "%08X" % (st.table_crc32(st.build_table(int(lat_e6), int(lon_e6), int(on), int(off))) & 0xFFFFFFFF)


def default_tbl(ss: int = 0) -> dict[str, Any]:
    """펌웨어 기본 표(부산, src 0) 조건 + CRC."""
    t = dict(DEFAULT_TBL_COND)
    t.update(src=SRC_FIRMWARE, ss=ss, crc=table_crc(t["lat_e6"], t["lon_e6"], t["on"], t["off"]))
    return t


def region_for_report(region: str) -> str:
    """8.4 — SETTINGS 로 보낼 때 따옴표·역슬래시·제어문자는 `?` 로 바꾼다."""
    return "".join("?" if (c in '"\\' or ord(c) < 0x20 or ord(c) == 0x7F) else c for c in region)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def stage_minutes(values: dict[str, int]) -> list[int]:
    return [int(values[f"stage{n}_h"]) * 60 + int(values[f"stage{n}_m"]) for n in (1, 2, 3, 4)]


def stage_order_ok(values: dict[str, int]) -> bool:
    """2.1 "1→2→3→4단계 시각이 저녁부터 밤 순서(자정 넘김 허용), 점등부터 4단계까지 24시간 미만".

    해석(가정 C2, 백엔드 `settings_rules` 와 맞춰 볼 것): 점등 시각은 날마다 달라 알 수 없으므로 1단계를 기점으로 본다.
    앞 단계에서 다음 단계까지 **앞으로 흐른 분**(자정 넘김 = +1440 을 법으로) d12, d23, d34 가 모두 0보다 크고
    합(1단계 → 4단계)이 1440분(24시간) 미만이면 통과. 같은 시각(d=0)은 순서가 아니므로 위반.
    """
    t = stage_minutes(values)
    deltas = [(t[i + 1] - t[i]) % MINUTES_PER_DAY for i in range(3)]
    return all(d > 0 for d in deltas) and sum(deltas) < MINUTES_PER_DAY


def check_rules(values: dict[str, int]) -> str | None:
    """2.1 규칙. 위반한 규칙 id(`ui_items.json` rules), 없으면 None."""
    if not int(values["cut12"]) < int(values["rtn12"]):
        return "batt12_order"
    if not int(values["cut24"]) < int(values["rtn24"]):
        return "batt24_order"
    if not stage_order_ok(values):
        return "stage_order"
    return None


def check_range(values: dict[str, int]) -> str | None:
    """범위 밖인 첫 key(ui_items min/max), 없으면 None."""
    for i in items():
        v = values[i["key"]]
        if not (int(i["min"]) <= v <= int(i["max"])):
            return i["key"]
    return None


def validate_settings_set(payload: dict[str, Any], current_values: dict[str, int]) -> tuple[str, str | None]:
    """SETTINGS_SET 검사(승인 상태·Flash 는 호출자). (결과, 문제 항목) — 결과는 OK / BAD / RANGE / RULE / CRC.

    순서: 형식(BAD: v 가 dict 아님·25개 중 누락·정수 아님, tbl 항목 누락·정수 아님·crc 없음) → 범위(RANGE: 25개,
    tbl 좌표·보정, region 47바이트 초과) → 규칙(RULE) → 표 CRC(CRC). 하나라도 틀리면 아무것도 바꾸지 않는다(8.4).
    `v` 의 모르는 키는 무시한다(가정 C3).
    """
    v = payload.get("v")
    if not isinstance(v, dict):
        return "BAD", "v"
    for k in keys():
        if k not in v:
            return "BAD", k
        if not _is_int(v[k]):
            return "BAD", k
    tbl = payload.get("tbl")
    if tbl is not None:
        if not isinstance(tbl, dict):
            return "BAD", "tbl"
        for k in TBL_KEYS:
            if k not in tbl:
                return "BAD", f"tbl.{k}"
        if not isinstance(tbl["region"], str):
            return "BAD", "tbl.region"
        for k in ("lat_e6", "lon_e6", "on", "off"):
            if not _is_int(tbl[k]):
                return "BAD", f"tbl.{k}"
        if not isinstance(tbl.get("crc"), str) or not tbl["crc"]:
            return "BAD", "tbl.crc"
    new_values = {k: int(v[k]) for k in keys()}
    bad = check_range(new_values)
    if bad is not None:
        return "RANGE", bad
    if tbl is not None:
        if abs(tbl["lat_e6"]) > TBL_LAT_MAX:
            return "RANGE", "tbl.lat_e6"
        if abs(tbl["lon_e6"]) > TBL_LON_MAX:
            return "RANGE", "tbl.lon_e6"
        if abs(tbl["on"]) > TBL_CORR_MAX:
            return "RANGE", "tbl.on"
        if abs(tbl["off"]) > TBL_CORR_MAX:
            return "RANGE", "tbl.off"
        if len(tbl["region"].encode("utf-8")) > REGION_MAX_BYTES:
            return "RANGE", "tbl.region"
    rule = check_rules(new_values)
    if rule is not None:
        return "RULE", rule
    if tbl is not None:
        expected = table_crc(tbl["lat_e6"], tbl["lon_e6"], tbl["on"], tbl["off"])
        if tbl["crc"].upper() != expected:
            return "CRC", f"tbl.crc {tbl['crc']} != {expected}"
    return "OK", None


def one_line(payload: dict[str, Any]) -> bytes:
    """서버가 보내야 하는 모양(8.4): 한 줄 JSON, `separators=(",",":")`, UTF-8 그대로."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
