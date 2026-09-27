"""S-23 단말 설정 규칙 (ADR-007, docs/spec/settings/UI_항목_명세.md 2장·8장) — DB 없이."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.core import settings_rules as r
from app.models.settings import DeviceSettings
from app.mqtt.publisher import check_size, encode
from app.mqtt.telemetry_buffer import override_clear_times
from app.vendor import suntable

REPO = Path(__file__).resolve().parents[2]
SPEC_JSON = REPO / "docs" / "spec" / "settings" / "ui_items.json"


def _values(**over: int) -> dict[str, int]:
    v = r.defaults()
    v.update(over)
    return v


def _stages(*times: tuple[int, int]) -> dict[str, int]:
    over = {}
    for n, (h, m) in enumerate(times, start=1):
        over[f"stage{n}_h"] = h
        over[f"stage{n}_m"] = m
    return _values(**over)


# ── 항목 정의 · 지문 · CRC ───────────────────────────────────────────────
@pytest.mark.skipif(not SPEC_JSON.exists(), reason="docs 없음(컨테이너 안)")
def test_vendor_ui_items_is_unmodified_copy():
    assert r.UI_ITEMS_PATH.read_bytes().splitlines() == SPEC_JSON.read_bytes().splitlines()


def test_items_order_and_count():
    keys = r.item_keys()
    assert len(keys) == 25
    assert keys[0] == "start_ofst" and keys[-1] == "rtn_time"
    assert keys[19:23] == ("cut12", "rtn12", "cut24", "rtn24")


def test_model_and_migration_columns_match_ui_items():
    cols = [c.name for c in DeviceSettings.__table__.columns]
    assert cols[1:26] == list(r.item_keys())
    mig = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
           / "0004_device_settings.py").read_text(encoding="utf-8")
    ns: dict = {}
    start = mig.index("_KEYS = (")
    exec(mig[start:mig.index(")\n", start) + 2], ns)  # noqa: S102 - 리비전 파일의 튜플 리터럴
    assert ns["_KEYS"] == r.item_keys()


def test_fingerprint_defaults():
    assert r.fingerprint(r.defaults()) == "38AF0DBD"


def test_fingerprint_changes_with_any_value_and_order_matters():
    base = r.fingerprint(r.defaults())
    assert r.fingerprint(_values(fade=11)) != base
    assert len(r.fingerprint(_values(start_ofst=-60))) == 8  # 음수도 <i 로


def test_table_crc_seoul():
    assert r.table_crc(37566500, 126978000, 0, 0) == "69C1DF86"


@pytest.mark.parametrize(("lat", "lon", "crc"), [
    (37881300, 127730000, "7BBB701A"),   # 춘천
    (35159500, 126852600, "063614CD"),   # 광주
    (35228000, 128681100, "145E32EB"),   # 창원
])
def test_table_crc_vectors(lat, lon, crc):
    assert r.table_crc(lat, lon, 0, 0) == crc


def test_schedule_preview_rows_and_index():
    rows = r.schedule_preview(37566500, 126978000, 0, 0)
    assert len(rows) == 24
    assert [(x["month"], x["day"]) for x in rows[:3]] == [(1, 1), (1, 15), (2, 1)]
    table = suntable.build_table(37566500, 126978000, 0, 0)
    on_h, on_m, off_h, off_m = table[r.table_index(7, 15)]
    july15 = rows[13]
    assert (july15["month"], july15["day"]) == (7, 15)
    assert july15["on"] == f"{on_h:02d}:{on_m:02d}" and july15["off"] == f"{off_h:02d}:{off_m:02d}"
    assert r.table_index(1, 1) == 0 and r.table_index(12, 31) == 371
    assert all(8 < x["hours"] < 16 for x in rows)
    # 겨울 밤이 여름 밤보다 길다
    assert rows[0]["hours"] > july15["hours"]


# ── 검사 ─────────────────────────────────────────────────────────────────
def test_defaults_pass_validation():
    assert r.validate_values(r.defaults()) == r.defaults()


def test_incomplete_and_unknown_keys():
    v = r.defaults()
    v.pop("fade")
    with pytest.raises(r.SettingsInvalid) as e:
        r.validate_values(v)
    assert e.value.code == "SETTINGS_INCOMPLETE" and e.value.detail["missing"] == ["fade"]
    with pytest.raises(r.SettingsInvalid) as e:
        r.validate_values({**r.defaults(), "fadee": 1})
    assert e.value.code == "SETTINGS_INCOMPLETE"


@pytest.mark.parametrize("item", r.items(), ids=lambda i: i["key"])
def test_range_edges_each_item(item):
    k = item["key"]
    for ok in (item["min"], item["max"]):
        r.check_ranges(_values(**{k: ok}))
    for bad in (item["min"] - 1, item["max"] + 1):
        with pytest.raises(r.SettingsInvalid) as e:
            r.check_ranges(_values(**{k: bad}))
        assert e.value.code == "SETTINGS_RANGE" and e.value.detail["key"] == k


def test_non_int_rejected_as_range():
    for bad in (True, 1.5, "10", None):
        with pytest.raises(r.SettingsInvalid) as e:
            r.validate_values({**r.defaults(), "fade": bad})
        assert e.value.code == "SETTINGS_RANGE"


@pytest.mark.parametrize(("over", "rule"), [
    ({"cut12": 1315, "rtn12": 1315}, "batt12_order"),
    ({"cut12": 1400, "rtn12": 1315}, "batt12_order"),
    ({"cut24": 2630, "rtn24": 2630}, "batt24_order"),
    ({"cut24": 2700, "rtn24": 2630}, "batt24_order"),
])
def test_battery_rules(over, rule):
    with pytest.raises(r.SettingsInvalid) as e:
        r.check_rules(_values(**over))
    assert e.value.code == "SETTINGS_RULE" and e.value.detail["rule"] == rule


@pytest.mark.parametrize(("times", "ok"), [
    (((20, 0), (0, 0), (2, 0), (4, 0)), True),       # 기본값 — 자정 넘김 한 번
    (((18, 0), (19, 0), (20, 0), (21, 0)), True),     # 넘김 없음
    (((22, 0), (23, 30), (1, 0), (3, 0)), True),      # 가운데서 넘김
    (((20, 0), (20, 1), (20, 2), (19, 59)), True),    # 1→4 간격 1439분(경계 안)
    (((20, 0), (22, 0), (23, 0), (20, 0)), False),    # 4단계 = 1단계 → 정확히 24시간
    (((20, 0), (20, 0), (21, 0), (22, 0)), False),    # 같은 시각 두 단계 = 한 바퀴
    (((20, 0), (19, 0), (21, 0), (22, 0)), False),    # 2단계가 1단계보다 이르다 → 24시간 넘음
    (((20, 0), (2, 0), (1, 0), (4, 0)), False),       # 넘긴 뒤 다시 거꾸로 → 두 바퀴
    (((0, 0), (6, 0), (12, 0), (23, 59)), True),      # 넘김 없이 하루 안
])
def test_stage_order_matrix(times, ok):
    v = _stages(*times)
    assert r.stage_order_ok(v) is ok
    if ok:
        r.check_rules(v)
    else:
        with pytest.raises(r.SettingsInvalid) as e:
            r.check_rules(v)
        assert e.value.detail["rule"] == "stage_order"


def test_unwrap_stages_defaults():
    assert r.unwrap_stages(r.defaults()) == [1200, 1440, 1560, 1680]


# ── region · 표 조건 ─────────────────────────────────────────────────────
def test_region_rules():
    assert r.region_problem("서울") is None
    assert r.region_problem("가" * 15 + "AB") is None             # 47 바이트
    assert len(("가" * 15 + "AB").encode()) == 47
    assert r.region_problem("가" * 16) is not None                # 48 바이트
    for bad in ('서"울', "서\\울", "서\n울", "서\x7f울", "서\x85울", "", "   ", None, 12):
        assert r.region_problem(bad) is not None, repr(bad)


def test_validate_table_rounds_and_ranges():
    t = r.validate_table("서울", 37.5665, 126.978, 0, 0)
    assert (t.lat_e6, t.lon_e6) == (37566500, 126978000)
    assert t.crc == "69C1DF86"
    for args, key in ((("서울", 90.1, 0, 0, 0), "tbl.lat"), (("서울", 0, -180.5, 0, 0), "tbl.lon"),
                      (("서울", 0, 0, 181, 0), "tbl.on"), (("서울", 0, 0, 0, -181), "tbl.off"),
                      (("서울", 0, 0, 1.5, 0), "tbl.on")):
        with pytest.raises(r.SettingsInvalid) as e:
            r.validate_table(*args)
        assert e.value.detail.get("key") == key
    with pytest.raises(r.SettingsInvalid) as e:
        r.validate_table('a"b', 0, 0, 0, 0)
    assert e.value.code == "VALIDATION_FAILED"


# ── payload ──────────────────────────────────────────────────────────────
def test_set_payload_shape_and_order():
    body = r.set_body(r.defaults(), None)
    p = r.set_payload(seq=502, body=body)
    assert list(p) == ["type", "seq", "v"]
    assert list(p["v"]) == list(r.item_keys())
    t = r.validate_table("서울", 37.5665, 126.978, 0, 0)
    p = r.set_payload(seq=502, body=r.set_body(r.defaults(), t))
    assert list(p) == ["type", "seq", "v", "tbl"]
    assert p["tbl"] == {"region": "서울", "lat_e6": 37566500, "lon_e6": 126978000,
                        "on": 0, "off": 0, "crc": "69C1DF86"}
    assert r.get_payload(seq=501) == {"type": "SETTINGS_GET", "seq": 501}


def test_set_payload_worst_case_size_under_900():
    """25개 전부 가장 긴 표기 + 한글 47바이트 region + 가장 긴 좌표·보정·seq."""
    widest = {}
    for item in r.items():
        widest[item["key"]] = max((item["min"], item["max"]), key=lambda x: len(str(x)))
    t = r.TableSpec(region="가" * 15 + "AB", lat_e6=-90_000_000, lon_e6=-180_000_000,
                    on=-180, off=-180)
    p = r.set_payload(seq=4_294_967_295, body=r.set_body(widest, t))
    raw = encode(p)
    assert b"\n" not in raw and b"\r" not in raw
    assert len(raw) < 900
    assert len(raw) < 620   # 명세 "최대 약 560B" 근처
    check_size("cmd", raw)


def test_encode_never_emits_newline():
    raw = encode({"region": "a\nb\rc"})
    assert b"\n" not in raw and b"\r" not in raw


# ── 상태 전이 (핸들러 판단) ──────────────────────────────────────────────
def test_report_first_read_same_changed():
    empty = {k: None for k in r.item_keys()}
    assert r.on_report(db_values=empty, reported_sh="38AF0DBD",
                       reported_values=r.defaults()) == r.READ_FIRST
    db = dict(r.defaults())
    assert r.on_report(db_values=db, reported_sh="38af0dbd", reported_values=None) == r.READ_SAME
    changed = _values(fade=5)
    assert r.on_report(db_values=db, reported_sh=r.fingerprint(changed),
                       reported_values=changed) == r.READ_CHANGED
    # sh 가 빠졌으면 보고 값으로 계산
    assert r.on_report(db_values=db, reported_sh=None, reported_values=changed) == r.READ_CHANGED
    assert r.on_report(db_values=db, reported_sh=None,
                       reported_values=r.defaults()) == r.READ_SAME
    partial = dict(db, fade=None)
    assert r.on_report(db_values=partial, reported_sh="X", reported_values=None) == r.READ_FIRST


def test_ack_decisions():
    sh = r.fingerprint(r.defaults())
    assert r.on_ack(result="OK", ack_sh=sh, sent_sh=sh) == r.ACK_APPLY
    assert r.on_ack(result="OK", ack_sh=sh.lower(), sent_sh=sh) == r.ACK_APPLY
    assert r.on_ack(result="OK", ack_sh="00000000", sent_sh=sh) == r.ACK_CHANGED
    for res in ("RANGE", "RULE", "CRC", "BAD", "STATE", "FLASH", "WHAT"):
        assert r.on_ack(result=res, ack_sh=sh, sent_sh=sh) == r.ACK_REJECTED
    assert r.on_ack(result="STATE", ack_sh=None, sent_sh=None) == r.ACK_REJECTED  # GET 거부


def test_restore_sync():
    assert r.restore_sync("synced") == "synced"
    assert r.restore_sync("local_saved") == "local_saved"
    assert r.restore_sync("unknown") == "unknown"
    assert r.restore_sync("writing") == "unknown"
    assert r.restore_sync(None) == "unknown"


@pytest.mark.parametrize(("attempts", "age", "trigger", "want"), [
    (1, 10, False, r.RESEND_WAIT),
    (1, 30, False, r.RESEND_NOW),
    (2, 31, False, r.RESEND_NOW),
    (3, 29, False, r.RESEND_WAIT),
    (3, 30, False, r.RESEND_GIVE_UP),
    (1, 4.9, True, r.RESEND_WAIT),      # 단말 송신 직후라도 5초는 기다린다
    (1, 5, True, r.RESEND_NOW),
    (2, 12, True, r.RESEND_NOW),
    (3, 12, True, r.RESEND_WAIT),       # 3회 다 씀 — 30초 뒤 TIMEOUT
    (3, 40, True, r.RESEND_GIVE_UP),
])
def test_resend_decision(attempts, age, trigger, want):
    assert r.resend_decision(attempts=attempts, sent_at_age_sec=age,
                             device_trigger=trigger) == want


def test_diff_lists_only_changed_in_order():
    db = r.defaults()
    dev = _values(fade=5, start_ofst=-10)
    assert r.diff(db, dev) == [{"key": "start_ofst", "db": 0, "device": -10},
                               {"key": "fade", "db": 10, "device": 5}]
    assert r.diff(None, dev) == []


# ── 5차 개정: md == 1 이면 override 지움 ─────────────────────────────────
T0 = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.timezone.utc)


def test_override_clear_times_md1_and_reboot():
    t1, t2, t3 = (T0 + dt.timedelta(seconds=s) for s in (1, 2, 3))
    batch = [("A", {"md": 2}, t1), ("A", {"md": 1}, t2), ("B", {"md": 2}, t1),
             ("C", {"md": "1"}, t3), ("D", {"md": 0}, t1)]
    events = [{"uuid": "D", "kind": "REBOOT", "received_at": t2},
              {"uuid": "B", "kind": "LOST", "received_at": t1}]
    assert override_clear_times(batch, events) == {"A": t2, "C": t3, "D": t2}


def test_extra_telemetry_after_ok_is_not_anomaly():
    """원격 OK 2초 뒤 추가 TM(§3.10.8)은 sq 가 이어진다 — 유실·재부팅이 아니다."""
    from app.mqtt import sq
    v = sq.judge(41, 42)
    assert v.lost == 0 and not v.reboot
