"""단말 운전 설정(S-23) 시나리오 — 읽고 나서 쓴다, 지문 `sh`, 조건만 주고받는 표 + 5차 개정(현장 우선)
(ADR-007, docs/05 "단말 설정 API", UI 명세 8장, 사양서 §3.10.8·§3.10.11).

`--phase 6`(또는 `--phase-only 6`) 없이는 SKIP(`settings` 플래그). 설정 REST 가 404/405 면 SKIP(백엔드 미배포).
단말은 펌웨어 2026-09-27-7 모델(`tools/sim/device.py` + `tools/sim/settings.py`): SETTINGS_GET → SETTINGS,
SETTINGS_SET → SETTINGS_ACK(OK·RANGE·RULE·CRC·BAD·STATE·FLASH), `local_save()` = 현장 저장(`ss`+1),
수신 1,024B 초과·줄바꿈 payload 는 읽지 못함, OK 뒤 2초에 Telemetry 1건 추가.

백엔드 재발송 주기·횟수는 `SETTINGS_RETRY_SEC`(기본 30)·`SETTINGS_MAX_ATTEMPTS`(기본 3) 환경 변수로 맞춘다(ADR-007).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable

from tools.scenarios.common import approve, make_devices, send_tm_and_wait, wait_rows
from tools.scenarios.framework import Ctx, Fail, scenario
from tools.scenarios.services import error_code
from tools.sim import settings as st
from tools.sim.device import SimDevice

#: 단말 응답 → 서버 DB 반영 상한(초).
SETTINGS_WAIT = 10.0
#: 백엔드 재발송 간격·최대 시도(ADR-007 "30초 뒤 새 seq, 최대 3회").
RETRY_SEC = float(os.environ.get("SETTINGS_RETRY_SEC") or 30)
MAX_ATTEMPTS = int(os.environ.get("SETTINGS_MAX_ATTEMPTS") or 3)
#: 재발송을 유도하려고 Telemetry 를 보내는 간격(초, §1.1.10 "단말이 보낸 직후").
KICK_EVERY = 3.0
#: 이 코드의 404 는 "기능 없음"이 아니라 정상 응답이다.
_REAL_404 = {"DEVICE_NOT_FOUND"}
BUSAN_CRC = st.default_tbl()["crc"]


# ── 공통 도우미 ──────────────────────────────────────────────────────────

def _user(ctx: Ctx) -> str:
    return ctx.s.env.admin_user


def _describe(r) -> str:
    return f"{r.status_code} {error_code(r) or r.text[:200]}"


def _skip_if_missing(ctx: Ctx, r, what: str) -> None:
    if r.status_code in (404, 405) and error_code(r) not in _REAL_404:
        ctx.skip(f"설정 REST 미구현/미배포 — {what}: {_describe(r)}")


def _detail(r) -> Any:
    try:
        err = r.json().get("error") or {}
    except (ValueError, AttributeError):
        return None
    return err.get("detail") if isinstance(err, dict) else None


async def settings_of(ctx: Ctx, uuid: str) -> dict:
    r = await ctx.s.rest.get_settings(uuid, _user(ctx))
    _skip_if_missing(ctx, r, "GET /api/devices/{uuid}/settings")
    if r.status_code != 200:
        raise Fail(f"GET settings {uuid} → {_describe(r)}")
    return r.json()


async def wait_settings(ctx: Ctx, uuid: str, pred: Callable[[dict], bool], *, what: str,
                        timeout: float = SETTINGS_WAIT) -> dict:
    last: dict = {}

    async def ok() -> bool:
        nonlocal last
        last = await settings_of(ctx, uuid)
        return bool(pred(last))
    try:
        await ctx.wait_until(ok, timeout=timeout, what=what)
    except Fail:
        raise Fail(f"{what}: 마지막 sync={last.get('sync')} pending={last.get('pending')} "
                   f"last_result={last.get('last_result')} sh_db={last.get('sh_db')} sh_device={last.get('sh_device')}"
                   ) from None
    return last


def idle(s: dict) -> bool:
    return not s.get("pending")


async def history(ctx: Ctx, uuid: str) -> list[dict]:
    r = await ctx.s.rest.settings_history(uuid, 500, _user(ctx))
    _skip_if_missing(ctx, r, "GET /api/devices/{uuid}/settings/history")
    if r.status_code != 200:
        raise Fail(f"history → {_describe(r)}")
    body = r.json()
    return body.get("items", []) if isinstance(body, dict) else list(body)


async def post_read(ctx: Ctx, d: SimDevice) -> dict:
    """`POST …/settings/read` → 202 `{seq, sent_at}`. 단말이 같은 seq 의 SETTINGS_GET 을 받을 때까지(무시해도 수신은 기록)."""
    r = await ctx.s.rest.read_settings(d.uuid, _user(ctx))
    _skip_if_missing(ctx, r, "POST /api/devices/{uuid}/settings/read")
    if r.status_code != 202:
        raise Fail(f"POST settings/read → {_describe(r)} (기대 202)")
    body = r.json()
    seq = body.get("seq")
    ctx.check(isinstance(seq, int) and seq > 0 and bool(body.get("sent_at")), f"read 응답 seq·sent_at: {body}")
    await ctx.wait_until(lambda: any(p.get("seq") == seq for p in d.settings_requests("SETTINGS_GET")),
                         timeout=5, interval=0.1, what=f"단말 SETTINGS_GET seq {seq} 수신")
    return body


async def read_until(ctx: Ctx, d: SimDevice, sync: str, *, timeout: float = SETTINGS_WAIT) -> dict:
    """읽기 요청 → 단말 SETTINGS → 서버가 `sync` 로 판정하고 대기 해제될 때까지."""
    body = await post_read(ctx, d)
    return await wait_settings(ctx, d.uuid, lambda s: idle(s) and s.get("sync") == sync and s.get("read_at"),
                               what=f"읽기 seq {body['seq']} → sync {sync}", timeout=timeout)


async def put(ctx: Ctx, d: SimDevice, values: dict[str, int], tbl: dict | None = None, *,
              expect: int = 202) -> Any:
    body = {"values": values, "tbl": tbl}
    r = await ctx.s.rest.put_settings(d.uuid, body, _user(ctx))
    _skip_if_missing(ctx, r, "PUT /api/devices/{uuid}/settings")
    if r.status_code != expect:
        raise Fail(f"PUT settings → {_describe(r)} (기대 {expect}) detail={_detail(r)}")
    return r


def set_rx(d: SimDevice, seq: int | None = None) -> list[tuple[bytes, dict]]:
    """단말이 받은 SETTINGS_SET (원문, payload)."""
    return [(raw, p) for (_, _, raw, p) in d.settings_rx
            if isinstance(p, dict) and p.get("type") == "SETTINGS_SET" and (seq is None or p.get("seq") == seq)]


async def wait_set(ctx: Ctx, d: SimDevice, seq: int) -> tuple[bytes, dict]:
    await ctx.wait_until(lambda: set_rx(d, seq), timeout=5, interval=0.1, what=f"단말 SETTINGS_SET seq {seq} 수신")
    return set_rx(d, seq)[0]


def ack_of(d: SimDevice, seq: int) -> dict | None:
    got = [p for p in d.settings_replies(seq) if p.get("type") == "SETTINGS_ACK"]
    return got[0] if got else None


def check_one_line(ctx: Ctx, raw: bytes, what: str) -> None:
    ctx.check(b"\n" not in raw and b"\r" not in raw, f"{what}: 한 줄 JSON(줄바꿈 없음)")
    ctx.check(len(raw) <= st.SERVER_PAYLOAD_MAX, f"{what}: {len(raw)}B ≤ {st.SERVER_PAYLOAD_MAX}B")


async def active_read(ctx: Ctx, n: int = 1, **flags: Any) -> list[SimDevice]:
    """ACTIVE 단말 n 대를 만들고 한 번씩 읽어 synced 로 둔다."""
    devs = await make_devices(ctx, n, **flags)
    await approve(ctx, devs)
    for d in devs:
        await read_until(ctx, d, "synced")
    return devs


async def tm(ctx: Ctx, d: SimDevice) -> dict:
    return await send_tm_and_wait(ctx, d, await ctx.s.db.now())


async def kick_until(ctx: Ctx, d: SimDevice, pred: Callable[[], Any], *, timeout: float, what: str) -> None:
    """KICK_EVERY 초마다 Telemetry 를 보내며 pred 가 참이 될 때까지(서버 재발송 유도). pred 는 async 가능."""
    started = time.monotonic()
    while True:
        v = pred()
        if asyncio.iscoroutine(v):
            v = await v
        if v:
            break
        if time.monotonic() - started >= timeout:
            raise Fail(f"{what}: {timeout:.0f}s 안에 충족되지 않음")
        await d.send_tm_now()
        await asyncio.sleep(KICK_EVERY)
    ctx.log(f"{what} ({time.monotonic() - started:.0f}s)")


def widest_values() -> dict[str, int]:
    """가장 긴 JSON 이 되는 값(부호·자릿수 최대) 중 규칙을 지키는 것."""
    return {"start_ofst": -60, "stop_ofst": -60, "start_pwm": 100, "manual_40w": 99, "manual_5w1": 99,
            "manual_5w2": 99, "fade": 20, "stage1_h": 18, "stage1_m": 30, "stage1_pwm": 100, "stage2_h": 22,
            "stage2_m": 45, "stage2_pwm": 100, "stage3_h": 23, "stage3_m": 50, "stage3_pwm": 100, "stage4_h": 23,
            "stage4_m": 55, "stage4_pwm": 100, "cut12": 1499, "rtn12": 1500, "cut24": 2999, "rtn24": 3000,
            "cut_time": 60, "rtn_time": 60}


#: 한글 15자(45B) + 2B = UTF-8 47바이트(상한).
REGION_47 = "가나다라마바사아자차카타파하거" + "12"


# ── S6-01 첫 읽기 ────────────────────────────────────────────────────────

@scenario("S6-01", "첫 읽기 — 새 단말은 unknown·쓰기 409, 읽으면 단말 값(기본값) 저장·sh 38AF0DBD·이력 25행", phase=6,
          requires="settings", timeout=120)
async def s6_01(ctx: Ctx) -> None:
    """`GET /api/settings/schema` = ui_items.json(25개 key 순서). 새 단말 A(PENDING): GET settings → sync unknown·values null·
    read_at null → PUT 409(PENDING 이라 INVALID_STATE 또는 SETTINGS_NOT_READ) → B(ACTIVE, 한 번도 안 읽음) PUT → 409
    `SETTINGS_NOT_READ`, 단말 SETTINGS_SET 수신 0 → A 읽기(PENDING 허용) → 202 seq → 단말 SETTINGS → sync synced,
    values == 단말 기본값, sh_db == sh_device == `38AF0DBD`, ss_known == 단말 ss, tbl 부산·src 0·crc 일치(matches),
    dev {8, 24}, pending 없음 → 이력 `device_read` 25행(key 25개)."""
    r = await ctx.s.rest.settings_schema(_user(ctx))
    _skip_if_missing(ctx, r, "GET /api/settings/schema")
    ctx.check(r.status_code == 200, f"schema → {_describe(r)}")
    schema_keys = [i["key"] for g in r.json().get("groups", []) for i in g.get("items", [])]
    ctx.check_eq(schema_keys, list(st.keys()), "schema 항목 = ui_items.json 순서")

    a, b = await make_devices(ctx, 2)
    await wait_rows(ctx, [a.uuid, b.uuid])
    s = await settings_of(ctx, a.uuid)
    ctx.check(s.get("sync") == "unknown" and s.get("values") is None and s.get("read_at") is None,
              f"새 단말 unknown·values null: sync={s.get('sync')} values={s.get('values')}")
    r = await ctx.s.rest.put_settings(a.uuid, {"values": st.defaults(), "tbl": None}, _user(ctx))
    _skip_if_missing(ctx, r, "PUT settings")
    ctx.check(r.status_code == 409 and error_code(r) in ("INVALID_STATE", "SETTINGS_NOT_READ"),
              f"PENDING·읽지 않음 PUT → 409: {_describe(r)}")

    await approve(ctx, [b])
    r = await ctx.s.rest.put_settings(b.uuid, {"values": st.defaults(), "tbl": None}, _user(ctx))
    ctx.check(r.status_code == 409 and error_code(r) == "SETTINGS_NOT_READ",
              f"ACTIVE·읽지 않음 PUT → 409 SETTINGS_NOT_READ: {_describe(r)}")
    await ctx.hold(1.0, "SETTINGS_SET 이 안 나갔는지 확인 전")
    ctx.check(not set_rx(a) and not set_rx(b), "읽기 전에는 단말에 SETTINGS_SET 없음(기본값을 가정해 쓰지 않는다)")

    s = await read_until(ctx, a, "synced")
    ctx.check_eq(s.get("values"), a.settings, "values == 단말 값")
    ctx.check_eq(s.get("values"), st.defaults(), "단말 값 = 출하 기본값")
    ctx.check(s.get("sh_db") == s.get("sh_device") == st.DEFAULT_SH, f"sh_db·sh_device = 38AF0DBD: {s.get('sh_db')} "
                                                                      f"{s.get('sh_device')}")
    ctx.check_eq(s.get("ss_known"), a.ss, "ss_known = 단말 ss")
    t = s.get("tbl") or {}
    ctx.check(t.get("region") == "부산" and t.get("src") == 0 and t.get("crc") == BUSAN_CRC
              and t.get("lat_e6") == 35179600 and t.get("matches") is True, f"tbl 부산·src 0·matches: {t}")
    ctx.check_eq(s.get("dev"), {"dip": 8, "bat": 24}, "dev")
    row = await ctx.s.db.fetchrow("SELECT sync, fade, sh_device FROM device_settings WHERE uuid=$1", a.uuid)
    ctx.check(row is not None and row["sync"] == "synced" and row["fade"] == 10, f"DB device_settings: {row}")
    hist = [h for h in await history(ctx, a.uuid) if h.get("by") == "device_read" and h.get("key") in st.keys()]
    ctx.check_eq(len(hist), 25, "이력 device_read 행 수")
    ctx.check_eq({h["key"] for h in hist}, set(st.keys()), "이력 key 25개")


# ── S6-02 현장 값 보존 ──────────────────────────────────────────────────

@scenario("S6-02", "현장 값 보존 — 서버 접속 전 현장에서 바꾼 값을 읽어 DB 에 그대로, 서버는 기본값을 쓰지 않는다", phase=6,
          requires="settings", timeout=120)
async def s6_02(ctx: Ctx) -> None:
    """접속 전에 `local_save`(PC 도구)로 fade 3·start_ofst -15·24V 2500/2600·1단계 19:00 + 표 군포(src 1)·ss 30→31 →
    접속(PENDING) → 읽기 → DB values == 단말 값, sh 일치, tbl 군포·src 1·matches, ss_known 31 → 승인(ACTIVE)·첫 TM →
    서버가 SETTINGS_SET 을 한 번도 보내지 않았다(단말 수신 0), sync synced 유지."""
    (d,) = await make_devices(ctx, 1, start=False, ss=30)
    d.local_save({"fade": 3, "start_ofst": -15, "cut24": 2500, "rtn24": 2600, "stage1_h": 19,
                  "tbl": {"region": "군포", "lat_e6": 37361500, "lon_e6": 126935200}})
    await d.start()
    ctx.check(await d.wait_connected(30), "접속")
    await wait_rows(ctx, [d.uuid])
    s = await read_until(ctx, d, "synced")
    ctx.check_eq(s.get("values"), d.settings, "DB values == 현장 값")
    ctx.check(s["values"]["fade"] == 3 and s["values"]["start_ofst"] == -15, "음수·바뀐 값 그대로")
    ctx.check(s.get("sh_db") == s.get("sh_device") == d.sh != st.DEFAULT_SH, f"sh 일치·기본값 아님: {d.sh}")
    t = s.get("tbl") or {}
    ctx.check(t.get("region") == "군포" and t.get("src") == 1 and t.get("crc") == d.tbl["crc"]
              and t.get("matches") is True, f"tbl 군포·src 1: {t}")
    ctx.check_eq(s.get("ss_known"), 31, "ss_known")
    await approve(ctx, [d])
    await tm(ctx, d)
    await ctx.hold(2.0, "승인 뒤 서버가 설정을 밀어내지 않는지")
    ctx.check(not set_rx(d) and d.stats.settings_set_rx == 0, "서버 → 단말 SETTINGS_SET 0회")
    s = await settings_of(ctx, d.uuid)
    ctx.check_eq(s.get("sync"), "synced", "승인·TM 뒤에도 synced(ss 같음)")
    ctx.check_eq(d.settings["fade"], 3, "단말 값 그대로")


# ── S6-03 쓰기 ──────────────────────────────────────────────────────────

@scenario("S6-03", "쓰기 — 25개 전부·한 줄·≤900B, ACK OK → synced·ss_known, 2초 뒤 추가 Telemetry", phase=6,
          requires="settings", timeout=120)
async def s6_03(ctx: Ctx) -> None:
    """ACTIVE·읽음 → PUT fade 5(25개 전부) → 202 seq·sh_expected(= 계산 지문)·payload_bytes → 단말이 받은 원문:
    key {type, seq, v}·v 25개·tbl 없음·한 줄·≤900B·payload_bytes 와 같음 → ACK OK → sync synced, values.fade 5,
    sh_db == sh_device == sh_expected, ss_known == 단말 ss, last_result OK → ACK 뒤 1.5~3.5초에 추가 Telemetry(ss 새 값)
    → 그 뒤에도 synced(서버 자신의 쓰기를 현장 저장으로 오판하지 않음) → 이력 server_write fade 10→5 1행."""
    (d,) = await active_read(ctx)
    values = dict(d.settings, fade=5)
    r = await put(ctx, d, values)
    body = r.json()
    seq = body.get("seq")
    ctx.check_eq(body.get("sh_expected"), st.settings_sh(values), "sh_expected = 보낸 값의 지문")
    raw, p = await wait_set(ctx, d, seq)
    check_one_line(ctx, raw, "SETTINGS_SET")
    ctx.check_eq(set(p), {"type", "seq", "v"}, "payload key(표를 안 바꾸면 tbl 없음)")
    ctx.check_eq(list(p["v"]), list(st.keys()), "v 25개(ui_items 순서)")
    ctx.check_eq(p["v"], values, "v 값")
    ctx.check_eq(body.get("payload_bytes"), len(raw), "payload_bytes = 실제 발행 크기")
    await ctx.wait_until(lambda: ack_of(d, seq), timeout=5, interval=0.1, what="단말 SETTINGS_ACK")
    ack = ack_of(d, seq)
    ack_at = next(t for (t, x) in d.settings_tx if x is ack)
    ctx.check(ack["result"] == "OK" and ack["ss"] == d.ss, f"ACK OK: {ack}")
    s = await wait_settings(ctx, d.uuid, lambda s: idle(s) and s.get("sync") == "synced" and s.get("last_result") == "OK"
                            and (s.get("values") or {}).get("fade") == 5, what="서버 반영 synced·fade 5")
    ctx.check(s.get("sh_db") == s.get("sh_device") == body["sh_expected"], "sh_db == sh_device == sh_expected")
    ctx.check_eq(s.get("ss_known"), d.ss, "ss_known = ACK ss")
    await ctx.wait_until(lambda: any(r == "settings_ok" for (_, r, _) in d.extra_tm_log), timeout=5, interval=0.1,
                         what="추가 Telemetry(settings_ok)")
    t_extra, _, extra = next(x for x in d.extra_tm_log if x[1] == "settings_ok")
    ctx.check(1.5 <= t_extra - ack_at <= 3.5, f"ACK 뒤 {t_extra - ack_at:.2f}s 에 추가 Telemetry(약 2초)")
    ctx.check(extra is not None and extra["ss"] == d.ss, f"추가 TM ss = 새 ss: {extra and extra['ss']}")
    await ctx.wait_until(lambda: ctx.s.db.fetchval("SELECT 1 FROM telemetry WHERE uuid=$1 AND sq=$2", d.uuid, extra["sq"]),
                         timeout=10, what="추가 TM 적재")
    await ctx.hold(1.5, "TM 반영")
    s = await settings_of(ctx, d.uuid)
    ctx.check_eq(s.get("sync"), "synced", "자기 쓰기 뒤 Telemetry ss 로 local_saved 가 되지 않음")
    ctx.check_eq(s.get("ss_telemetry"), d.ss, "ss_telemetry")
    rows = [h for h in await history(ctx, d.uuid) if h.get("by") == "server_write"]
    ctx.check([(h.get("key"), h.get("old"), h.get("new")) for h in rows] == [("fade", 10, 5)],
              f"이력 server_write: {rows}")


# ── S6-04 서버 검사 ─────────────────────────────────────────────────────

@scenario("S6-04", "서버 검사 — 누락·범위·cut≥rtn·다단계 순서·region 따옴표·47B 초과 → 422, 단말에 안 감", phase=6,
          requires="settings", timeout=120)
async def s6_04(ctx: Ctx) -> None:
    """ACTIVE·읽음. PUT: rtn_time 누락 → 422 SETTINGS_INCOMPLETE / start_ofst 61 → 422 SETTINGS_RANGE(detail.key) /
    cut24 == rtn24 → 422 SETTINGS_RULE / 2단계 19:00(1단계 20:00 보다 앞) → 422 SETTINGS_RULE / tbl region 에 `"` → 422 /
    region 48바이트 → 422 / tbl on 181 → 422. 전부 단말 SETTINGS_SET 0·서버 계정 sniffer 에도 없음·DB 값 불변·synced."""
    (d,) = await active_read(ctx)
    sn = await ctx.s.broker.sniffer(d.topic("cmd"), name=f"{ctx.scenario.id}-cmd")
    ctx.defer(sn.stop)
    base = dict(d.settings)
    good_tbl = {"region": "서울", "lat": 37.5665, "lon": 126.978, "on": 0, "off": 0}
    missing = dict(base)
    missing.pop("rtn_time")
    cases: list[tuple[str, dict, dict | None, tuple[str, ...]]] = [
        ("rtn_time 누락", missing, None, ("SETTINGS_INCOMPLETE",)),
        ("start_ofst 61", dict(base, start_ofst=61), None, ("SETTINGS_RANGE",)),
        ("cut24 == rtn24", dict(base, cut24=2630, rtn24=2630), None, ("SETTINGS_RULE",)),
        ("다단계 순서", dict(base, stage2_h=19), None, ("SETTINGS_RULE",)),
        ('region 에 "', base, dict(good_tbl, region='서"울'), ()),
        ("region 48B", base, dict(good_tbl, region="가" * 16), ()),
        ("on 181", base, dict(good_tbl, on=181), ()),
    ]
    t0 = time.monotonic()
    for what, values, tbl, codes in cases:
        r = await ctx.s.rest.put_settings(d.uuid, {"values": values, "tbl": tbl}, _user(ctx))
        _skip_if_missing(ctx, r, "PUT settings")
        ok = r.status_code == 422 and (not codes or error_code(r) in codes)
        ctx.check(ok, f"{what} → 422 {codes or '(코드 무관)'}: {_describe(r)} detail={_detail(r)}")
        if what == "start_ofst 61":
            det = _detail(r)
            ctx.log(f"(참고) RANGE detail = {det}")
            if isinstance(det, dict) and "key" in det:
                ctx.check_eq(det["key"], "start_ofst", "detail.key")
    await ctx.hold(1.0, "발행이 없었는지 확인 전")
    ctx.check(not set_rx(d), "단말 SETTINGS_SET 0")
    ctx.check(not sn.find(type_="SETTINGS_SET", since=t0), "sniffer SETTINGS_SET 0")
    s = await settings_of(ctx, d.uuid)
    ctx.check(s.get("values") == base and s.get("sync") == "synced" and idle(s), f"DB 불변·synced: {s.get('sync')}")


# ── S6-05 단말 거부 ─────────────────────────────────────────────────────

@scenario("S6-05", "단말 거부 — 서버를 우회한 SETTINGS_SET 은 RANGE/RULE/BAD/CRC/STATE, DB 불변. FLASH 는 결과만", phase=6,
          requires="settings", timeout=150)
async def s6_05(ctx: Ctx) -> None:
    """서버 계정으로 원시 SETTINGS_SET(서버 DB 시퀀스와 안 겹치는 큰 seq): fade 21 → RANGE, cut12 1400/rtn12 1300 → RULE,
    fade 누락 → BAD, 서울 표 crc 00000000 → CRC, PENDING 단말 → STATE. 단말 값 불변, 서버는 모르는 seq 의 ACK 에
    DB 를 바꾸지 않는다(values·sync·sh 그대로), /health ok. 이어서 `settings_flash_fail_next=1` 로 PUT fade 5 →
    last_result FLASH·sync synced(원래대로)·values.fade 10 그대로·pending 없음 → 다시 PUT → OK."""
    (d,) = await active_read(ctx)
    (p,) = await make_devices(ctx, 1, offset=1)
    await wait_rows(ctx, [p.uuid])
    before = await settings_of(ctx, d.uuid)
    v = dict(d.settings)
    no_fade = dict(v)
    no_fade.pop("fade")
    seoul_bad = {"region": "서울", "lat_e6": 37566500, "lon_e6": 126978000, "on": 0, "off": 0, "crc": "00000000"}
    base_seq = 3_900_000_000 + (int(time.time()) % 1_000_000) * 10
    cases = [
        (d, "RANGE", {"v": dict(v, fade=21)}),
        (d, "RULE", {"v": dict(v, cut12=1400, rtn12=1300)}),
        (d, "BAD", {"v": no_fade}),
        (d, "CRC", {"v": v, "tbl": seoul_bad}),
        (p, "STATE", {"v": v}),
    ]
    for i, (dev, want, extra) in enumerate(cases):
        seq = base_seq + i
        await ctx.s.broker.publish(dev.topic("cmd"), {"type": "SETTINGS_SET", "seq": seq, **extra}, qos=1)
        await ctx.wait_until(lambda: ack_of(dev, seq), timeout=5, interval=0.1, what=f"원시 SETTINGS_SET → {want}")
        ctx.check_eq(ack_of(dev, seq)["result"], want, f"단말 결과 seq {seq}")
    ctx.check(d.settings == v and d.ss == before.get("ss_known"), "단말 값·ss 불변")
    await ctx.hold(1.5, "서버가 모르는 seq 의 ACK 처리")
    s = await settings_of(ctx, d.uuid)
    ctx.check(s.get("values") == before.get("values") and s.get("sync") == "synced"
              and s.get("sh_db") == before.get("sh_db"), f"DB 불변: sync={s.get('sync')}")
    ctx.check(await ctx.s.rest.health_ok(), "/health ok")

    d.settings_flash_fail_next = 1
    body = (await put(ctx, d, dict(v, fade=5))).json()
    await ctx.wait_until(lambda: ack_of(d, body["seq"]), timeout=5, interval=0.1, what="단말 FLASH 응답")
    ctx.check_eq(ack_of(d, body["seq"])["result"], "FLASH", "단말 결과")
    s = await wait_settings(ctx, d.uuid, lambda s: idle(s) and s.get("last_result") == "FLASH",
                            what="서버 last_result FLASH")
    ctx.check(s.get("sync") == "synced" and s["values"]["fade"] == 10 and d.settings["fade"] == 10,
              f"FLASH → DB·단말 값 그대로, sync synced: {s.get('sync')} fade {s['values']['fade']}")
    body = (await put(ctx, d, dict(v, fade=5))).json()
    await wait_settings(ctx, d.uuid, lambda s: idle(s) and s.get("last_result") == "OK" and s["values"]["fade"] == 5,
                        what="다시 쓰기 OK")
    ctx.check_eq(d.settings["fade"], 5, "단말 fade")


# ── S6-06 표 조건 ───────────────────────────────────────────────────────

@scenario("S6-06", "표 조건 — 서울 37.5665/126.978 → crc 69C1DF86, 단말 src 2, matches, 미리보기 crc 같음", phase=6,
          requires="settings", timeout=120)
async def s6_06(ctx: Ctx) -> None:
    """ACTIVE·읽음(부산 src 0). PUT 값 그대로 + tbl {서울, 37.5665, 126.978, 0, 0} → 단말 원문 tbl = {region 서울(UTF-8 그대로),
    lat_e6 37566500, lon_e6 126978000, on 0, off 0, crc 69C1DF86} → ACK OK → 단말 src 2·ss+1 → GET tbl.crc 69C1DF86·
    matches → 다시 읽기 → src 2·region 서울 → `GET /api/schedule/preview?lat=37.5665&lon=126.978` crc 69C1DF86·24행."""
    (d,) = await active_read(ctx)
    ss0 = d.ss
    body = (await put(ctx, d, dict(d.settings), {"region": "서울", "lat": 37.5665, "lon": 126.978, "on": 0, "off": 0})).json()
    raw, p = await wait_set(ctx, d, body["seq"])
    check_one_line(ctx, raw, "SETTINGS_SET(+tbl)")
    ctx.check_eq(p.get("tbl"), {"region": "서울", "lat_e6": 37566500, "lon_e6": 126978000, "on": 0, "off": 0,
                                "crc": st.SEOUL_TBL_CRC}, "payload tbl")
    ctx.check("서울".encode("utf-8") in raw, "region UTF-8 그대로(\\u 이스케이프 아님)")
    await ctx.wait_until(lambda: ack_of(d, body["seq"]), timeout=5, interval=0.1, what="SETTINGS_ACK")
    ctx.check_eq(ack_of(d, body["seq"])["result"], "OK", "단말 결과")
    ctx.check(d.tbl["src"] == 2 and d.tbl["crc"] == st.SEOUL_TBL_CRC and d.ss == ss0 + 1, f"단말 표: {d.tbl} ss {d.ss}")
    s = await wait_settings(ctx, d.uuid, lambda s: idle(s) and s.get("last_result") == "OK", what="쓰기 반영")
    t = s.get("tbl") or {}
    ctx.check(t.get("crc") == st.SEOUL_TBL_CRC and t.get("matches") is True, f"GET tbl crc·matches: {t}")
    s = await read_until(ctx, d, "synced")
    t = s.get("tbl") or {}
    ctx.check(t.get("src") == 2 and t.get("region") == "서울" and t.get("crc_expected") in (None, st.SEOUL_TBL_CRC),
              f"다시 읽기 tbl src 2·서울: {t}")
    r = await ctx.s.rest.schedule_preview(37.5665, 126.978, 0, 0, _user(ctx))
    _skip_if_missing(ctx, r, "GET /api/schedule/preview")
    ctx.check(r.status_code == 200, f"preview → {_describe(r)}")
    pv = r.json()
    ctx.check(pv.get("crc") == st.SEOUL_TBL_CRC and pv.get("lat_e6") == 37566500 and len(pv.get("rows") or []) == 24,
              f"preview crc·lat_e6·24행: {pv.get('crc')} {pv.get('lat_e6')} {len(pv.get('rows') or [])}")


# ── S6-07 현장 저장 감지 ────────────────────────────────────────────────

@scenario("S6-07", "현장 저장 감지 — 값은 그대로 ss 만 +1 → local_saved → 다시 읽기 → sh 같음 → synced", phase=6,
          requires="settings", timeout=120)
async def s6_07(ctx: Ctx) -> None:
    """ACTIVE·synced → `local_save({})`(cfg save, 값 같음) → TM(ss+1) → sync local_saved·ss_telemetry = 단말 ss·자동으로
    다시 읽지 않음(SETTINGS_GET 추가 없음) → 읽기 → sh 같음 → synced·ss_known = 단말 ss·값 이력 추가 없음."""
    (d,) = await active_read(ctx)
    n_vals = len([h for h in await history(ctx, d.uuid) if h.get("key") in st.keys()])
    n_get = len(d.settings_requests("SETTINGS_GET"))
    d.local_save({})
    await tm(ctx, d)
    s = await wait_settings(ctx, d.uuid, lambda s: s.get("sync") == "local_saved", what="sync local_saved")
    ctx.check_eq(s.get("ss_telemetry"), d.ss, "ss_telemetry")
    await ctx.hold(1.5, "자동 읽기가 없는지")
    ctx.check_eq(len(d.settings_requests("SETTINGS_GET")), n_get, "서버가 자동으로 다시 읽지 않음")
    s = await read_until(ctx, d, "synced")
    ctx.check(s.get("sh_device") == s.get("sh_db") == d.sh and s.get("ss_known") == d.ss,
              f"sh 같음·ss_known {s.get('ss_known')} == {d.ss}")
    after = len([h for h in await history(ctx, d.uuid) if h.get("key") in st.keys()])
    ctx.check_eq(after, n_vals, "값이 같으면 값 이력 추가 없음(행 수)")


# ── S6-08 현장 변경 ─────────────────────────────────────────────────────

@scenario("S6-08", "현장 변경 — device_changed·diff → 받아들이기(DB ← 단말), 또 바꾸면 되돌리기(SETTINGS_SET) → synced", phase=6,
          requires="settings", timeout=150)
async def s6_08(ctx: Ctx) -> None:
    """ACTIVE·synced(fade 10) → `local_save({"fade":3})` → TM → local_saved → 읽기 → device_changed·diff [fade 10→3]·
    report.fade 3·DB values.fade 10 그대로(덮어쓰지 않음) → accept → synced·fade 3·이력 by=사용자 → `local_save({"fade":7,
    "cut_time":20})` → TM → local_saved → 읽기 → device_changed(diff fade·cut_time) → revert → 단말이 받은 SETTINGS_SET v ==
    DB 값(fade 3, cut_time 10) → 단말 되돌아감 → synced·sh 같음 → 할 게 없으면 accept 409 SETTINGS_NOT_CHANGED."""
    (d,) = await active_read(ctx)
    d.local_save({"fade": 3})
    await tm(ctx, d)
    await wait_settings(ctx, d.uuid, lambda s: s.get("sync") == "local_saved", what="local_saved")
    s = await read_until(ctx, d, "device_changed")
    diff = {x.get("key"): (x.get("db"), x.get("device")) for x in s.get("diff") or []}
    ctx.check_eq(diff, {"fade": (10, 3)}, "diff")
    ctx.check((s.get("report") or {}).get("fade") == 3 and s["values"]["fade"] == 10,
              f"report.fade 3·DB values.fade 10 유지: {(s.get('report') or {}).get('fade')} {s['values']['fade']}")
    ctx.check(s.get("sh_device") == d.sh != s.get("sh_db"), "sh_device ≠ sh_db")
    r = await ctx.s.rest.accept_settings(d.uuid, _user(ctx))
    _skip_if_missing(ctx, r, "POST settings/accept")
    ctx.check(r.status_code in (200, 202), f"accept → {_describe(r)}")
    s = await wait_settings(ctx, d.uuid, lambda s: s.get("sync") == "synced" and s["values"]["fade"] == 3,
                            what="accept → synced·fade 3")
    ctx.check(s.get("sh_db") == s.get("sh_device") == d.sh, "sh 같음")
    acc = [h for h in await history(ctx, d.uuid) if h.get("key") == "fade" and h.get("new") == 3]
    ctx.check(any(h.get("by") == _user(ctx) for h in acc), f"이력 by = 사용자({_user(ctx)}): {acc}")

    d.local_save({"fade": 7, "cut_time": 20})
    await tm(ctx, d)
    await wait_settings(ctx, d.uuid, lambda s: s.get("sync") == "local_saved", what="local_saved 2")
    s = await read_until(ctx, d, "device_changed")
    diff = {x.get("key"): (x.get("db"), x.get("device")) for x in s.get("diff") or []}
    ctx.check_eq(diff, {"fade": (3, 7), "cut_time": (10, 20)}, "diff 2")
    db_values = dict(s["values"])
    n_set = len(set_rx(d))
    r = await ctx.s.rest.revert_settings(d.uuid, _user(ctx))
    _skip_if_missing(ctx, r, "POST settings/revert")
    ctx.check(r.status_code in (200, 202), f"revert → {_describe(r)}")
    await ctx.wait_until(lambda: len(set_rx(d)) > n_set, timeout=5, interval=0.1, what="revert → SETTINGS_SET")
    raw, p = set_rx(d)[-1]
    check_one_line(ctx, raw, "revert SETTINGS_SET")
    ctx.check_eq(p["v"], db_values, "revert v == DB 값")
    await ctx.wait_until(lambda: ack_of(d, p["seq"]), timeout=5, interval=0.1, what="revert ACK")
    ctx.check(d.settings["fade"] == 3 and d.settings["cut_time"] == 10, "단말이 DB 값으로 돌아감")
    s = await wait_settings(ctx, d.uuid, lambda s: idle(s) and s.get("sync") == "synced", what="revert → synced")
    ctx.check(s.get("sh_db") == s.get("sh_device") == d.sh and s.get("ss_known") == d.ss, "sh·ss_known 일치")
    r = await ctx.s.rest.accept_settings(d.uuid, _user(ctx))
    ctx.check(r.status_code == 409 and error_code(r) == "SETTINGS_NOT_CHANGED", f"할 게 없는 accept → 409: {_describe(r)}")


# ── S6-09 응답 없음 재발송 ──────────────────────────────────────────────

@scenario("S6-09", "응답 없음 — 새 seq 로 재발송(단말 TM 직후 또는 30초), 3회 무응답이면 TIMEOUT·대기 해제", phase=6,
          requires="settings", timeout=420)
async def s6_09(ctx: Ctx) -> None:
    """ACTIVE·synced. `settings_silent_next=1` → 읽기 → 단말 무시 → TM 을 보내며 기다리면 서버가 **새 seq** 로 SETTINGS_GET
    재발송 → 응답 → synced. `settings_silent_next=5` → 읽기 → TM 을 보내며 기다리면 `SETTINGS_MAX_ATTEMPTS`(3)회까지만
    보내고(seq 전부 다름) last_result TIMEOUT·pending 없음·sync 원래대로(synced)."""
    (d,) = await active_read(ctx)
    d.settings_silent_next = 1
    body = await post_read(ctx, d)
    seq1 = body["seq"]
    await kick_until(ctx, d, lambda: len([p for p in d.settings_requests("SETTINGS_GET") if p["seq"] >= seq1]) >= 2,
                     timeout=RETRY_SEC + 40, what="새 seq 재발송")
    seqs = [p["seq"] for p in d.settings_requests("SETTINGS_GET") if p["seq"] >= seq1]
    ctx.check(len(set(seqs)) == len(seqs) and seqs[1] > seq1, f"재발송 seq 새것: {seqs}")
    s = await wait_settings(ctx, d.uuid, lambda s: idle(s) and s.get("sync") == "synced", what="재발송 응답 → synced")
    ctx.log(f"첫 요청 seq {seq1} → 응답한 seq {seqs[1]}, last_result={s.get('last_result')}")

    d.settings_silent_next = 5
    body = await post_read(ctx, d)
    seq0 = body["seq"]

    async def timed_out() -> bool:
        s = await settings_of(ctx, d.uuid)
        return idle(s) and s.get("last_result") == "TIMEOUT"
    await kick_until(ctx, d, timed_out, timeout=RETRY_SEC * (MAX_ATTEMPTS + 1) + 60, what="3회 무응답 → TIMEOUT")
    seqs = [p["seq"] for p in d.settings_requests("SETTINGS_GET") if p["seq"] >= seq0]
    ctx.check(len(seqs) == MAX_ATTEMPTS and len(set(seqs)) == MAX_ATTEMPTS, f"시도 {MAX_ATTEMPTS}회·seq 모두 다름: {seqs}")
    s = await settings_of(ctx, d.uuid)
    ctx.check(s.get("sync") in ("synced",), f"TIMEOUT 뒤 sync 원래대로: {s.get('sync')}")
    d.settings_silent_next = 0


# ── S6-10 크기 ──────────────────────────────────────────────────────────

@scenario("S6-10", "크기 — 한글 region 47B + 가장 긴 값 → ≤900B 한 줄, 단말 OK", phase=6, requires="settings", timeout=120)
async def s6_10(ctx: Ctx) -> None:
    """ACTIVE·읽음. 값: 음수 offset·4자리 전압·2자리 시각 등 가장 긴 모양(규칙 지킴) + tbl {region 한글 15자+2 = 47B,
    lat -33.868888, lon -151.209999, on -180, off -180} → PUT 202 payload_bytes ≤ 900 → 단말 원문 한 줄·≤900B·tbl.crc =
    suntable 계산값 → ACK OK → synced·단말 region 그대로."""
    ctx.check_eq(len(REGION_47.encode("utf-8")), 47, "region 47바이트")
    (d,) = await active_read(ctx)
    values = widest_values()
    ctx.check(st.check_rules(values) is None and st.check_range(values) is None, "시험값이 규칙·범위 안")
    tbl = {"region": REGION_47, "lat": -33.868888, "lon": -151.209999, "on": -180, "off": -180}
    body = (await put(ctx, d, values, tbl)).json()
    ctx.check(isinstance(body.get("payload_bytes"), int) and body["payload_bytes"] <= st.SERVER_PAYLOAD_MAX,
              f"payload_bytes {body.get('payload_bytes')} ≤ 900")
    raw, p = await wait_set(ctx, d, body["seq"])
    check_one_line(ctx, raw, "최대 SETTINGS_SET")
    ctx.log(f"최대 SETTINGS_SET {len(raw)}B")
    t = p.get("tbl") or {}
    ctx.check(t.get("lat_e6") == -33868888 and t.get("lon_e6") == -151209999, f"좌표 round(x*1e6): {t}")
    ctx.check_eq(t.get("crc"), st.table_crc(-33868888, -151209999, -180, -180), "tbl.crc = suntable")
    await ctx.wait_until(lambda: ack_of(d, body["seq"]), timeout=5, interval=0.1, what="SETTINGS_ACK")
    ctx.check_eq(ack_of(d, body["seq"])["result"], "OK", "단말 결과")
    ctx.check(d.settings == values and d.tbl["region"] == REGION_47, "단말 적용")
    s = await wait_settings(ctx, d.uuid, lambda s: idle(s) and s.get("sync") == "synced" and s.get("last_result") == "OK",
                            what="synced")
    ctx.check_eq(s.get("values"), values, "DB values")


# ── S6-11 5차 개정: 현장 우선 ────────────────────────────────────────────

@scenario("S6-11", "5차 개정 — 원격 OK 뒤 2초 TM, 현장 시작 → 원격 취소·md 1 TM·서버 override 해제, LOCAL 은 버림", phase=6,
          requires=("settings", "group_cmd"), timeout=180)
async def s6_11(ctx: Ctx) -> None:
    """ACTIVE. 개별 off dur 600 → ACK OK → 1.5~3.5초 뒤 추가 TM(md 2) → 서버 remote_active true → 단말 `start_local()` →
    슬롯 전부 취소·약 2초 뒤 TM md 1 → 서버가 override 를 지움(remote_active false·override_* NULL) → 현장 중 개별 on →
    `LOCAL`·종결(target LOCAL) → `end_local()` → md 0·아무것도 적용 안 됨(보류 없음) → 새 명령 on dur 5 → OK·md 2 →
    유지시간 끝 → 약 2초 뒤 추가 TM(md 0)."""
    from tools.scenarios.phase5 import rest_device, send, target, wait_acks, wait_counts
    devs = await make_devices(ctx, 1)
    await approve(ctx, devs)
    (d,) = devs
    await tm(ctx, d)

    b1 = await send(ctx, target("device", d.uuid), "off", dur=600)
    await wait_acks(ctx, [d], b1["seq"], "OK")
    ack_at = next(t for (t, _, a) in d.ack_log if a.get("seq") == b1["seq"])
    await ctx.wait_until(lambda: any(r == "command_ok" for (_, r, _) in d.extra_tm_log), timeout=5, interval=0.1,
                         what="OK 뒤 추가 TM")
    t_extra, _, extra = next(x for x in d.extra_tm_log if x[1] == "command_ok")
    ctx.check(1.5 <= t_extra - ack_at <= 3.5 and extra and extra["md"] == 2,
              f"ACK 뒤 {t_extra - ack_at:.2f}s 추가 TM md {extra and extra['md']}")

    async def remote(active: bool) -> dict | None:
        dv = await rest_device(ctx, d.uuid)
        return dv if dv.get("remote_active") is active else None
    await ctx.wait_until(lambda: remote(True), timeout=10, what="서버 remote_active true")

    n = len(d.extra_tm_log)
    ctx.check_eq(d.start_local(), 1, "현장 시작 → 원격 슬롯 1개 취소")
    ctx.check(d.slot_dump() == {} and d.md == 1, "슬롯 없음·md 1")
    await ctx.wait_until(lambda: len(d.extra_tm_log) > n, timeout=5, interval=0.1, what="현장 취소 뒤 추가 TM")
    _, why, extra = d.extra_tm_log[-1]
    ctx.check(why == "local" and extra and extra["md"] == 1, f"추가 TM md 1: {why} {extra and extra['md']}")
    dv = await ctx.wait_until(lambda: remote(False), timeout=10, what="md 1 → 서버 override 해제")
    ctx.check(dv.get("override_act") is None and dv.get("override_until") is None,
              f"override_* NULL: {dv.get('override_act')} {dv.get('override_until')}")

    b2 = await send(ctx, target("device", d.uuid), "on", dur=300)
    await wait_acks(ctx, [d], b2["seq"], "LOCAL")
    await wait_counts(ctx, b2["seq"], {"LOCAL": 1, "pending": 0}, finished=True)
    d.end_local()
    ctx.check(d.slot_dump() == {} and d.md == 0, "현장 종료 → 스케줄(LOCAL 명령 적용 안 함)")
    t = await tm(ctx, d)
    ctx.check_eq(t["md"], 0, "TM md 0")
    await ctx.hold(1.0, "뒤늦게 적용되지 않는지")
    ctx.check(d.slot_dump() == {}, "여전히 슬롯 없음")

    n = len(d.extra_tm_log)
    b3 = await send(ctx, target("device", d.uuid), "on", dur=5)
    await wait_acks(ctx, [d], b3["seq"], "OK")
    ctx.check(d.effective_act == "on" and d.md == 2, "새 명령 OK·적용")
    await ctx.wait_until(lambda: any(r == "expiry" for (_, r, _) in d.extra_tm_log[n:]), timeout=15, interval=0.2,
                         what="유지시간 끝 → 추가 TM")
    _, _, extra = next(x for x in d.extra_tm_log[n:] if x[1] == "expiry")
    ctx.check(extra is not None and extra["md"] == 0, f"만료 뒤 TM md 0: {extra and extra['md']}")
