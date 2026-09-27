"""단말 운전 설정(S-23) 규칙 — 항목 · 지문 `sh` · 표 CRC · 검사 · payload · 상태 전이 (ADR-007).

전부 순수 함수다(DB·브로커·시계 없음). 서비스(API)·수신 핸들러·재발송 타이머가 같은 규칙을 쓴다.

항목 정의의 유일한 기준은 `app/vendor/ui_items.json`(= docs/spec/settings/ui_items.json 사본, 고치지
않는다). 키·**순서**·범위·기본값·배율을 거기서 읽는다. 순서가 지문 계산 순서다(명세 8.6).

다단계 순서 규칙(명세 2.1 "1→2→3→4단계 시각이 저녁부터 밤 순서(자정 넘김 허용), 점등부터 4단계까지
24시간 미만")의 서버 해석:
  · 각 단계 시각을 분(h*60+m)으로 바꾸고 1단계부터 차례로 **펼친다** — 다음 단계가 앞 단계보다
    같거나 이르면 자정을 넘긴 것으로 보고 +1440 한다(한 번 더 넘기면 또 +1440).
  · 펼친 시각이 **엄격히 증가**해야 하고(같은 시각 두 단계 = 자정 한 바퀴로 간주 → 아래에서 걸린다),
  · 1단계 → 4단계 간격이 **1440분 미만**이어야 한다.
  서버는 일몰(점등) 시각을 단계 판정에 쓰지 않는다 — 점등은 날마다 바뀌고 1단계가 곧 저녁 첫 단계다.
  경계 밖 조합은 단말이 `RULE` 로 거부하므로(명세 8.4) 서버 검사는 "대부분 미리 막기" 용도다.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.vendor import suntable

UI_ITEMS_PATH = Path(__file__).resolve().parents[1] / "vendor" / "ui_items.json"

#: sync 상태(docs/03 device_settings.sync).
SYNC_UNKNOWN = "unknown"
SYNC_SYNCED = "synced"
SYNC_WRITING = "writing"
SYNC_LOCAL_SAVED = "local_saved"
SYNC_DEVICE_CHANGED = "device_changed"
SYNC_STATES = (SYNC_UNKNOWN, SYNC_SYNCED, SYNC_WRITING, SYNC_LOCAL_SAVED, SYNC_DEVICE_CHANGED)

KIND_GET = "SETTINGS_GET"
KIND_SET = "SETTINGS_SET"

#: SETTINGS_ACK.result (명세 8.4). TIMEOUT 은 서버가 붙이는 값(3회 무응답).
ACK_RESULTS = frozenset({"OK", "RANGE", "RULE", "CRC", "BAD", "STATE", "FLASH"})
RESULT_TIMEOUT = "TIMEOUT"

#: 재발송 — 30초 무응답이면 새 seq 로, 최대 3회(첫 발송 포함). 단말 송신 직후는 5초만 지났어도.
RESEND_AFTER_SEC = 30
RESEND_MAX_ATTEMPTS = 3
RESEND_ON_DEVICE_MIN_SEC = 5

REGION_MAX_BYTES = 47
LAT_E6_MAX = 90_000_000
LON_E6_MAX = 180_000_000
CORR_MIN = -180
CORR_MAX = 180

#: 표 조건 키(SETTINGS_SET.tbl, crc 제외) — 순서 고정.
TBL_KEYS = ("region", "lat_e6", "lon_e6", "on", "off")


# ── 항목 정의 ────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    """ui_items.json 원본(dict). `GET /api/settings/schema` 가 그대로 돌려준다."""
    return json.loads(UI_ITEMS_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def items() -> tuple[dict[str, Any], ...]:
    """25개 항목, ui_items.json 순서 그대로."""
    return tuple(i for g in schema()["groups"] for i in g["items"])


@lru_cache(maxsize=1)
def item_keys() -> tuple[str, ...]:
    return tuple(i["key"] for i in items())


def defaults() -> dict[str, int]:
    return {i["key"]: int(i["default"]) for i in items()}


# ── 지문 · 표 CRC ────────────────────────────────────────────────────────
def fingerprint(values: dict[str, int]) -> str:
    """설정 지문 `sh`(명세 8.6) — 25개를 ui_items 순서대로 `<i` 로 이어 CRC-32, 대문자 8자리."""
    raw = b"".join(struct.pack("<i", int(values[k])) for k in item_keys())
    return f"{zlib.crc32(raw) & 0xFFFFFFFF:08X}"


@lru_cache(maxsize=256)
def table_crc(lat_e6: int, lon_e6: int, on: int, off: int) -> str:
    """스케줄 표 CRC(명세 8.3) — suntable 372칸 표의 CRC-32, 대문자 8자리."""
    return f"{suntable.table_crc32(suntable.build_table(lat_e6, lon_e6, on, off)):08X}"


@lru_cache(maxsize=256)
def _table(lat_e6: int, lon_e6: int, on: int, off: int) -> tuple[tuple[int, int, int, int], ...]:
    return tuple(tuple(r) for r in suntable.build_table(lat_e6, lon_e6, on, off))


def table_index(month: int, day: int) -> int:
    """표 칸 번호 = (월-1)*31 + (일-1) (suntable, 월마다 31칸)."""
    return (month - 1) * suntable.DAYS_PER_MONTH + (day - 1)


def schedule_preview(lat_e6: int, lon_e6: int, on: int, off: int) -> list[dict[str, Any]]:
    """매달 1일·15일 24행: 점등(on)·소등(off) "HH:MM", 점등 시간(hours, 소수 1자리).

    한 칸 = (점등 시, 분, 소등 시, 분). 소등은 다음 날 아침이다(소등 ≤ 점등이면 +24h)."""
    table = _table(lat_e6, lon_e6, on, off)
    rows = []
    for month in range(1, 13):
        for day in (1, 15):
            on_h, on_m, off_h, off_m = table[table_index(month, day)]
            start, end = on_h * 60 + on_m, off_h * 60 + off_m
            if end <= start:
                end += 1440
            rows.append({
                "month": month, "day": day, "on": f"{on_h:02d}:{on_m:02d}",
                "off": f"{off_h:02d}:{off_m:02d}", "hours": round((end - start) / 60, 1),
            })
    return rows


# ── 검사 ─────────────────────────────────────────────────────────────────
class SettingsInvalid(ValueError):
    """code = SETTINGS_INCOMPLETE / SETTINGS_RANGE / SETTINGS_RULE / VALIDATION_FAILED."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_complete(values: dict[str, Any]) -> dict[str, int]:
    """25개 전부 정수인가. 빠진 키 → SETTINGS_INCOMPLETE. 모르는 키는 무시하지 않고 거부한다
    (오타가 조용히 빠지면 사용자는 바꿨다고 믿는다)."""
    missing = [k for k in item_keys() if k not in values]
    if missing:
        raise SettingsInvalid("SETTINGS_INCOMPLETE", "25개 항목이 모두 있어야 합니다",
                              missing=missing)
    unknown = sorted(set(values) - set(item_keys()))
    if unknown:
        raise SettingsInvalid("SETTINGS_INCOMPLETE", "모르는 항목이 있습니다", unknown=unknown)
    for k in item_keys():
        if not _is_int(values[k]):
            raise SettingsInvalid("SETTINGS_RANGE", f"{k} 는 정수여야 합니다", key=k)
    return {k: int(values[k]) for k in item_keys()}


def check_ranges(values: dict[str, int]) -> None:
    for item in items():
        v = values[item["key"]]
        if not item["min"] <= v <= item["max"]:
            raise SettingsInvalid(
                "SETTINGS_RANGE", f"{item['key']} 범위 밖 ({item['min']}~{item['max']})",
                key=item["key"], min=item["min"], max=item["max"], value=v,
            )


def unwrap_stages(values: dict[str, int]) -> list[int]:
    """1~4단계 시각(분)을 1단계 기준으로 펼친다(모듈 docstring)."""
    out: list[int] = []
    for n in range(1, 5):
        t = values[f"stage{n}_h"] * 60 + values[f"stage{n}_m"]
        while out and t <= out[-1]:
            t += 1440
        out.append(t)
    return out


def stage_order_ok(values: dict[str, int]) -> bool:
    t = unwrap_stages(values)
    return t[-1] - t[0] < 1440


def check_rules(values: dict[str, int]) -> None:
    """명세 2.1 — cut12<rtn12, cut24<rtn24, 다단계 순서·24시간."""
    if not values["cut12"] < values["rtn12"]:
        raise SettingsInvalid("SETTINGS_RULE", "12V 차단 전압은 복귀 전압보다 작아야 합니다",
                              rule="batt12_order")
    if not values["cut24"] < values["rtn24"]:
        raise SettingsInvalid("SETTINGS_RULE", "24V 차단 전압은 복귀 전압보다 작아야 합니다",
                              rule="batt24_order")
    if not stage_order_ok(values):
        raise SettingsInvalid(
            "SETTINGS_RULE", "1→4단계 시각이 순서대로(자정 넘김 허용) 24시간 안이어야 합니다",
            rule="stage_order",
        )


def validate_values(values: dict[str, Any]) -> dict[str, int]:
    """완전성 → 범위 → 규칙. 통과하면 ui_items 순서의 int dict."""
    clean = check_complete(values)
    check_ranges(clean)
    check_rules(clean)
    return clean


def region_problem(region: Any) -> str | None:
    """region 이 단말에 보낼 수 있는가. 문제 설명 또는 None."""
    if not isinstance(region, str):
        return "문자열이어야 합니다"
    if not region.strip():
        return "비어 있습니다"
    if len(region.encode("utf-8")) > REGION_MAX_BYTES:
        return f"UTF-8 {REGION_MAX_BYTES}바이트를 넘습니다"
    for ch in region:
        o = ord(ch)
        if ch in ('"', "\\") or o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F:
            return "따옴표·역슬래시·제어문자는 쓸 수 없습니다"
    return None


@dataclass(frozen=True)
class TableSpec:
    region: str
    lat_e6: int
    lon_e6: int
    on: int
    off: int

    @property
    def crc(self) -> str:
        return table_crc(self.lat_e6, self.lon_e6, self.on, self.off)

    def payload(self) -> dict[str, Any]:
        return {"region": self.region, "lat_e6": self.lat_e6, "lon_e6": self.lon_e6,
                "on": self.on, "off": self.off, "crc": self.crc}


def validate_table(region: Any, lat: Any, lon: Any, on: Any, off: Any) -> TableSpec:
    """PUT 의 tbl(실수 좌표) → TableSpec. lat/lon 은 round(x*1e6)."""
    problem = region_problem(region)
    if problem:
        raise SettingsInvalid("VALIDATION_FAILED", f"region: {problem}", field="tbl.region")
    try:
        lat_e6 = round(float(lat) * 1_000_000)
        lon_e6 = round(float(lon) * 1_000_000)
    except (TypeError, ValueError) as e:
        raise SettingsInvalid("VALIDATION_FAILED", "lat/lon 은 숫자", field="tbl.lat") from e
    if not -LAT_E6_MAX <= lat_e6 <= LAT_E6_MAX:
        raise SettingsInvalid("SETTINGS_RANGE", "위도는 -90~90", key="tbl.lat")
    if not -LON_E6_MAX <= lon_e6 <= LON_E6_MAX:
        raise SettingsInvalid("SETTINGS_RANGE", "경도는 -180~180", key="tbl.lon")
    for name, v in (("on", on), ("off", off)):
        if not _is_int(v) or not CORR_MIN <= v <= CORR_MAX:
            raise SettingsInvalid("SETTINGS_RANGE", f"{name} 보정은 -180~180 정수",
                                  key=f"tbl.{name}")
    return TableSpec(region=region, lat_e6=lat_e6, lon_e6=lon_e6, on=int(on), off=int(off))


# ── payload ──────────────────────────────────────────────────────────────
def get_payload(*, seq: int) -> dict[str, Any]:
    return {"type": KIND_GET, "seq": seq}


def set_body(values: dict[str, int], table: TableSpec | None) -> dict[str, Any]:
    """SETTINGS_SET 에서 seq 를 뺀 몸통 — `v`(25개 ui_items 순서) + 표를 바꿀 때만 `tbl`."""
    body: dict[str, Any] = {"v": {k: int(values[k]) for k in item_keys()}}
    if table is not None:
        body["tbl"] = table.payload()
    return body


def set_payload(*, seq: int, body: dict[str, Any]) -> dict[str, Any]:
    """`{"type":"SETTINGS_SET","seq","v":{…},"tbl"?:{…}}` — 키 순서 고정."""
    payload: dict[str, Any] = {"type": KIND_SET, "seq": seq, "v": body["v"]}
    if body.get("tbl") is not None:
        payload["tbl"] = body["tbl"]
    return payload


def payload_for(kind: str, *, seq: int, body: dict[str, Any] | None) -> dict[str, Any]:
    if kind == KIND_GET:
        return get_payload(seq=seq)
    return set_payload(seq=seq, body=body or {})


# ── 단말 보고 해석 ───────────────────────────────────────────────────────
def parse_values(v: Any) -> dict[str, int] | None:
    """SETTINGS.v → 25개 int dict. 하나라도 없거나 정수가 아니면 None."""
    if not isinstance(v, dict):
        return None
    out: dict[str, int] = {}
    for k in item_keys():
        x = v.get(k)
        if not _is_int(x):
            return None
        out[k] = int(x)
    return out


def values_complete(values: dict[str, Any | None]) -> bool:
    return all(values.get(k) is not None for k in item_keys())


def diff(db: dict[str, Any] | None, device: dict[str, Any] | None) -> list[dict[str, Any]]:
    """두 값 묶음에서 다른 key 만 ui_items 순서로."""
    if not db or not device:
        return []
    return [{"key": k, "db": db.get(k), "device": device.get(k)} for k in item_keys()
            if db.get(k) != device.get(k)]


# ── 상태 전이 (핸들러가 쓰는 판단만 순수하게) ────────────────────────────
READ_FIRST = "first_read"
READ_SAME = "synced"
READ_CHANGED = "device_changed"


def on_report(*, db_values: dict[str, Any | None], reported_sh: str | None,
              reported_values: dict[str, int] | None) -> str:
    """SETTINGS 수신 → first_read / synced / device_changed.

    DB 에 25개가 다 없으면 첫 읽기(단말 값을 그대로 저장). 있으면 DB 값으로 계산한 지문을 단말 `sh`
    와 비교한다. 단말이 `sh` 를 빠뜨렸으면 보고된 값으로 계산한 지문을 쓴다.
    """
    if not values_complete(db_values):
        return READ_FIRST
    sh = (reported_sh or "").upper() or (
        fingerprint(reported_values) if reported_values else ""
    )
    db_sh = fingerprint({k: int(db_values[k]) for k in item_keys()})
    return READ_SAME if sh == db_sh else READ_CHANGED


ACK_APPLY = "apply"          # DB ← 보낸 값, synced
ACK_CHANGED = "changed"      # OK 인데 sh 다름 → device_changed
ACK_REJECTED = "rejected"    # RANGE/RULE/…: DB 그대로, sync 원래대로


def on_ack(*, result: str, ack_sh: str | None, sent_sh: str | None) -> str:
    """SETTINGS_ACK 판단. sent_sh 는 SET 으로 보낸 값의 지문(GET 에 대한 STATE 면 None)."""
    if result != "OK":
        return ACK_REJECTED
    if sent_sh is None:
        return ACK_REJECTED  # GET 에 OK 는 오지 않는다 — 방어
    return ACK_APPLY if (ack_sh or "").upper() == sent_sh else ACK_CHANGED


def restore_sync(prev: str | None) -> str:
    """writing 을 끝낼 때 돌아갈 상태. 기록이 없거나 writing 이면 unknown/synced 로 보수적으로."""
    if prev in SYNC_STATES and prev != SYNC_WRITING:
        return prev
    return SYNC_UNKNOWN


# ── 재발송 자격 ──────────────────────────────────────────────────────────
RESEND_WAIT = "wait"
RESEND_NOW = "resend"
RESEND_GIVE_UP = "timeout"


def resend_decision(*, attempts: int, sent_at_age_sec: float, device_trigger: bool) -> str:
    """응답 대기 중인 요청을 지금 어떻게 할까.

    · 30초 지났고 시도 < 3 → 새 seq 로 재발송. 30초 지났고 3회 다 썼으면 → TIMEOUT.
    · 단말이 방금 뭔가 보냈고(§1.1.10) 마지막 발송에서 5초 이상 지났고 시도 < 3 → 바로 재발송.
    """
    if sent_at_age_sec >= RESEND_AFTER_SEC:
        return RESEND_NOW if attempts < RESEND_MAX_ATTEMPTS else RESEND_GIVE_UP
    if device_trigger and sent_at_age_sec >= RESEND_ON_DEVICE_MIN_SEC \
            and attempts < RESEND_MAX_ATTEMPTS:
        return RESEND_NOW
    return RESEND_WAIT
