"""스케줄 배포 규칙(순수 함수) — ADR-010, 사양서 §13.1.

프로필 = 표 조건(region·lat·lon·on·off → crc) + 운전 15개. 15개 = ui_items.json 의 그룹
`schedule`(시작/종료 Offset·시작 밝기) + `stage`(다단계 1~4 시·분·밝기). 키를 여기 적지 않고 스키마에서 뽑는다.
나머지 10개(기준 밝기 3·fade·배터리 6)는 단말마다 다르므로 배포 때 그 단말의 **마지막 읽은 값**을 그대로 보낸다.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.core import settings_rules as rules

#: 프로필에 들어가는 ui_items 그룹 id.
PROFILE_GROUPS = ("schedule", "stage")


def profile_keys() -> tuple[str, ...]:
    keys: list[str] = []
    for g in rules.schema()["groups"]:
        if g["id"] in PROFILE_GROUPS:
            keys += [it["key"] for it in g["items"]]
    return tuple(keys)


def profile_defaults() -> dict[str, int]:
    d = rules.defaults()
    return {k: d[k] for k in profile_keys()}


def validate_profile_values(values: dict[str, Any]) -> dict[str, int]:
    """15개 전부 정수·범위·다단계 순서. 모르는 키·빠진 키는 거부(SettingsInvalid)."""
    keys = profile_keys()
    missing = [k for k in keys if k not in values]
    if missing:
        raise rules.SettingsInvalid("SETTINGS_INCOMPLETE", "프로필 운전 항목 15개가 모두 있어야 합니다",
                                    missing=missing)
    unknown = sorted(set(values) - set(keys))
    if unknown:
        raise rules.SettingsInvalid("SETTINGS_INCOMPLETE", "프로필에 넣을 수 없는 항목입니다",
                                    unknown=unknown)
    clean: dict[str, int] = {}
    by_key = {it["key"]: it for it in rules.items()}
    for k in keys:
        v = values[k]
        if not isinstance(v, int) or isinstance(v, bool):
            raise rules.SettingsInvalid("SETTINGS_RANGE", f"{k} 는 정수여야 합니다", key=k)
        it = by_key[k]
        if not it["min"] <= v <= it["max"]:
            raise rules.SettingsInvalid(
                "SETTINGS_RANGE", f"{k} 범위 밖 ({it['min']}~{it['max']})", key=k,
                min=it["min"], max=it["max"], value=v)
        clean[k] = v
    if not rules.stage_order_ok({**rules.defaults(), **clean}):
        raise rules.SettingsInvalid(
            "SETTINGS_RULE", "1→4단계 시각이 순서대로(자정 넘김 허용) 24시간 안이어야 합니다",
            rule="stage_order")
    return clean


def overlay(base: dict[str, Any], profile_values: dict[str, int]) -> dict[str, int]:
    """단말 마지막 읽은 25개 + 프로필 15개 덮기 → 25개 검증(범위·규칙). 실패는 SettingsInvalid."""
    merged = {**{k: base.get(k) for k in rules.item_keys()}, **profile_values}
    return rules.validate_values(merged)


def clean_region(region: str) -> str:
    """앞뒤 공백·BOM 제거(§13.1 [필수])."""
    return region.replace("﻿", "").strip()


def effective_profile(
    uuid_assign: int | None, node_chain: Iterable[int | None], node_assign: dict[int, int],
) -> tuple[int | None, str | None]:
    """단말 → (프로필 id, 출처). 단말 배정이 먼저, 없으면 말단부터 위로 가장 가까운 노드 배정.

    node_chain = 말단 → 시군구 → 시도(단말의 node_id 부터 조상 순). 출처 = "device" / "node:<id>" / None.
    """
    if uuid_assign is not None:
        return uuid_assign, "device"
    for node_id in node_chain:
        if node_id is not None and node_id in node_assign:
            return node_assign[node_id], f"node:{node_id}"
    return None, None


def item_result(ack_result: str | None) -> str:
    """SETTINGS_SET 최종 결과 → deploy_item.status. TIMEOUT(3회 무응답) = NO_RESPONSE."""
    r = (ack_result or "").upper()
    if r in rules.ACK_RESULTS:
        return r
    if r == rules.RESULT_TIMEOUT:
        return "NO_RESPONSE"
    return "BAD" if r else "NO_RESPONSE"


def needs_read(sync: str | None, values: dict[str, Any] | None) -> bool:
    """배포 전에 SETTINGS_GET 이 필요한가 — 서버가 단말 값을 모르거나(unknown), 현장에서 바뀌었을 수 있으면
    (local_saved·device_changed) 먼저 읽는다. 기준 밝기·배터리 값을 덮지 않으려는 것(§13.1 ③)."""
    if sync in (None, rules.SYNC_UNKNOWN, rules.SYNC_LOCAL_SAVED, rules.SYNC_DEVICE_CHANGED):
        return True
    return not values or not rules.values_complete(values)


# ── 배포 한 항목의 다음 걸음 (deploy_runner 가 틱마다 부른다) ─────────────
STEP_WAIT = "wait"        # 그대로 둔다(오프라인·다른 요청 진행 중·응답 대기)
STEP_READ = "read"        # SETTINGS_GET 먼저
STEP_WRITE = "write"      # SETTINGS_SET(25개 + tbl)
STEP_FINISH = "finish"    # 끝 — (status, detail)


def next_step(
    *, status: str, device_state: str | None, is_online: bool, busy: bool,
    request_result: str | None, sync: str | None, values: dict[str, Any] | None,
) -> tuple[str, str | None, str | None]:
    """(걸음, 끝 status, 설명). request_result = 이 항목이 낸 요청의 최종 결과(아직이면 None).

    · ACTIVE 가 아니면 STATE 로 끝(단말도 STATE 로 거부한다 — 보내지 않는다).
    · 읽는 중·보낸 뒤: 결과를 기다린다. 읽기 OK → 쓰기, 읽기 실패 → NO_RESPONSE/READ_FAILED.
      쓰기 결과 → OK / CRC / RULE / RANGE / BAD / STATE / FLASH / NO_RESPONSE(3회 무응답).
    · 대기: 오프라인이거나 다른 설정 요청이 진행 중이면 기다린다(다시 접속하면 자동, §13.1).
      서버가 단말 값을 모르거나 현장에서 바뀌었을 수 있으면 읽기부터.
    """
    if device_state != "ACTIVE":
        return STEP_FINISH, "STATE", f"승인 상태 {device_state or '없음'} — ACTIVE 만 배포"
    if status in ("reading", "sent"):
        if request_result is None:
            return STEP_WAIT, None, None
        if status == "reading":
            if request_result.upper() == "OK":
                return STEP_WRITE, None, None
            if request_result.upper() == rules.RESULT_TIMEOUT:
                return STEP_FINISH, "NO_RESPONSE", "읽기(SETTINGS_GET) 3회 무응답"
            return STEP_FINISH, "READ_FAILED", f"읽기 결과 {request_result}"
        return STEP_FINISH, item_result(request_result), request_result
    if busy or not is_online:
        return STEP_WAIT, None, None
    if needs_read(sync, values):
        return STEP_READ, None, None
    return STEP_WRITE, None, None
