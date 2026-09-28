"""5차 COMMAND 규칙 — 검증 · 유지시간 · topic · 재시도 자격 · 종료 판정 · override (ADR-005).

전부 순수 함수다(DB·브로커·시계 없음 — 시각은 인자로 받는다). 서비스·수신 핸들러·타이머가
같은 규칙을 쓰도록 한 곳에 둔다. 사양서 §3.10.7(payload), §3.10.8(우선순위·슬롯),
§3.10.11(응답·재시도), §17(act).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.constants import (
    COMMAND_ACTS,
    COMMAND_CHANNELS,
    COMMAND_DEFAULT_CH,
    DUR_MAX_SEC,
    DUR_MIN_SEC,
    DUR_PRESET_TONIGHT,
    DUR_PRESETS,
    RETRYABLE_STATUSES,
    TERMINAL_STATUSES,
    OverrideLevel,
    TargetStatus,
)
from app.core.region_tree import Tree
from app.mqtt import topics
from app.mqtt.publisher import KST
from app.vendor import suntable

EXP_MIN_SEC = 1
EXP_MAX_SEC = 3600
PWM_MIN = 0
PWM_MAX = 100


class CommandInvalid(ValueError):
    """요청 검증 실패. 서비스가 422 VALIDATION_FAILED 로 바꾼다."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class CommandSpec:
    """검증을 통과한 명령 내용. dur 은 tonight 이면 아직 None(좌표를 알아야 계산)."""

    act: str
    ch: tuple[int, ...]
    pwm: tuple[int, ...] | None
    dur: int | None
    dur_preset: str | None
    exp: int

    @property
    def needs_tonight(self) -> bool:
        return self.act != "auto" and self.dur_preset == DUR_PRESET_TONIGHT


# ── 검증 ─────────────────────────────────────────────────────────────────
def validate_command(
    *, act: str, ch: list[int] | None, pwm: list[int] | None, dur: int | None,
    dur_preset: str | None, exp: int | None, default_exp: int,
) -> CommandSpec:
    """docs/05 "명령" 공통 본문 규칙.

    · act ∈ on/off/pwm/auto
    · ch 기본 [1,2], 값 1~3, 중복 없음, 비어 있으면 안 됨(빠지면 단말은 "모든 채널"로 보지만
      화면은 항상 명시한다 — 의도하지 않은 3번 채널 조작을 막는다)
    · pwm 은 act=pwm 일 때만·필수, ch 와 같은 길이, 각 0~100 (다르면 단말이 BAD)
    · act≠auto 면 dur(1~86400) 또는 dur_preset 중 **하나** 필수. auto 는 둘 다 없어야 한다
    · exp 기본 COMMAND_EXP_SEC, 1~3600
    """
    act = (act or "").strip().lower()
    if act not in COMMAND_ACTS:
        raise CommandInvalid("act", f"act 는 {list(COMMAND_ACTS)} 중 하나")

    channels = list(COMMAND_DEFAULT_CH) if ch is None else list(ch)
    if not channels:
        raise CommandInvalid("ch", "ch 가 비어 있다")
    if any(c not in COMMAND_CHANNELS for c in channels):
        raise CommandInvalid("ch", f"ch 값은 {list(COMMAND_CHANNELS)} 중에서")
    if len(set(channels)) != len(channels):
        raise CommandInvalid("ch", "ch 에 같은 채널이 두 번 있다")

    levels: tuple[int, ...] | None = None
    if act == "pwm":
        if pwm is None:
            raise CommandInvalid("pwm", "act=pwm 이면 pwm 이 필요하다")
        if len(pwm) != len(channels):
            raise CommandInvalid("pwm", "pwm 길이가 ch 와 다르다(단말이 BAD 로 거부)")
        if any(not isinstance(v, int) or isinstance(v, bool) or not PWM_MIN <= v <= PWM_MAX
               for v in pwm):
            raise CommandInvalid("pwm", f"pwm 값은 {PWM_MIN}~{PWM_MAX}")
        levels = tuple(pwm)
    elif pwm is not None:
        raise CommandInvalid("pwm", "pwm 은 act=pwm 일 때만")

    preset = dur_preset.strip().lower() if isinstance(dur_preset, str) and dur_preset.strip() \
        else None
    if act == "auto":
        if dur is not None or preset is not None:
            raise CommandInvalid("dur", "act=auto 는 유지시간이 없다(즉시 스케줄 복귀)")
        seconds = None
    else:
        if (dur is None) == (preset is None):
            raise CommandInvalid("dur", "dur 또는 dur_preset 중 하나만 주어야 한다")
        if dur is not None:
            if not DUR_MIN_SEC <= dur <= DUR_MAX_SEC:
                raise CommandInvalid("dur", f"dur 은 {DUR_MIN_SEC}~{DUR_MAX_SEC}초")
            seconds = dur
        elif preset == DUR_PRESET_TONIGHT:
            seconds = None  # 대상 좌표로 나중에 계산
        elif preset in DUR_PRESETS:
            seconds = DUR_PRESETS[preset]
        else:
            raise CommandInvalid(
                "dur_preset", f"dur_preset 은 {[*DUR_PRESETS, DUR_PRESET_TONIGHT]} 중 하나"
            )

    exp_value = default_exp if exp is None else exp
    if not EXP_MIN_SEC <= exp_value <= EXP_MAX_SEC:
        raise CommandInvalid("exp", f"exp 는 {EXP_MIN_SEC}~{EXP_MAX_SEC}초")

    return CommandSpec(act=act, ch=tuple(channels), pwm=levels, dur=seconds,
                       dur_preset=preset, exp=exp_value)


# ── "오늘 밤" (suntable, 사양서 §3.10.7 · docs/spec/ref) ─────────────────
@lru_cache(maxsize=256)
def _table(lat_e6: int, lon_e6: int) -> tuple[tuple[int, int, int, int], ...]:
    # 372칸 표 계산은 CORDIC 수백 번이라 좌표별로 기억해 둔다(말단 수천 개여도 몇 MB).
    return tuple(suntable.build_table(lat_e6, lon_e6, 0, 0))


def off_time(day: dt.date, lat: float, lon: float) -> dt.time:
    """그날 소등 시각(KST). 표 한 칸 = (점등 시, 분, **소등** 시, 분) — 소등 = 일출 쪽(아침).

    보정(off_corr)은 0 — 단말 설치 보정값은 6차 SCH 에서 서버가 알게 된다. 그때 여기에 넣는다.
    """
    table = _table(round(lat * 1_000_000), round(lon * 1_000_000))
    _, _, off_h, off_m = table[(day.month - 1) * suntable.DAYS_PER_MONTH + (day.day - 1)]
    return dt.time(off_h, off_m)


def tonight_seconds(now: dt.datetime, lat: float, lon: float) -> int:
    """지금(aware)부터 **다음** 소등 시각까지 남은 초, 1~86400.

    오늘 소등(아침)이 아직 안 지났으면(새벽) 오늘 것, 지났으면(낮·저녁) 내일 것. 저녁에 "오늘 밤
    끄기"를 누르면 내일 아침 소등까지 = 원래 스케줄이 다시 켜질 일이 없는 시각까지 유지된다.
    """
    local = now.astimezone(KST)
    today = local.date()
    candidate = dt.datetime.combine(today, off_time(today, lat, lon), tzinfo=KST)
    if candidate <= local:
        tomorrow = today + dt.timedelta(days=1)
        candidate = dt.datetime.combine(tomorrow, off_time(tomorrow, lat, lon), tzinfo=KST)
    seconds = int((candidate - local).total_seconds() + 0.999)  # 올림 — 1분 모자라 켜지지 않게
    return max(DUR_MIN_SEC, min(DUR_MAX_SEC, seconds))


# ── topic ────────────────────────────────────────────────────────────────
def command_topics(kind: str, target_id: str | None, tree: Tree) -> list[str]:
    """대상 → 발행 topic 목록 (§3.10.4 표).

    device          device/<uuid>/cmd 1회
    node(말단)       group/<bjd>00/cmd 1회
    node(상위)       하위 말단 group topic 마다 1회(단말은 한 그룹만 구독 — 중복 수신 없음)
    all             all/cmd 1회
    """
    if kind == "device":
        return [topics.device_cmd(str(target_id))]
    if kind == "all":
        return [topics.all_cmd()]
    if kind == "node":
        node_id = int(str(target_id))
        return [topics.group_cmd(leaf.grp) for leaf in tree.leaves_under(node_id) if leaf.grp]
    raise ValueError(f"알 수 없는 target kind: {kind}")


def override_level_for(target_kind: str) -> str:
    """명령 대상 → 단말 override 슬롯 계층(§3.10.8)."""
    return {
        "device": OverrideLevel.DEVICE.value,
        "node": OverrideLevel.GROUP.value,
        "all": OverrideLevel.ALL.value,
    }[target_kind]


def command_valid_until(sent_at: dt.datetime, dur: int | None, timeout_sec: int) -> dt.datetime:
    """재시도해도 의미 있는 시각 상한 — `sent_at + dur`, auto(dur 없음)는 + COMMAND_TIMEOUT."""
    return sent_at + dt.timedelta(seconds=dur if dur else timeout_sec)


# ── 자동 재시도 자격 (ADR-005 — 단말이 뭔가 보낸 직후) ───────────────────
def retry_eligible(
    *, status: str, attempts: int, last_sent_at: dt.datetime | None,
    cmd_sent_at: dt.datetime, cmd_dur: int | None, cmd_finished: bool, now: dt.datetime,
    max_attempts: int, retry_min_sec: int, timeout_sec: int, superseded: bool = False,
) -> bool:
    """SQL 판(mqtt/command_retry.claim_targets)과 같은 규칙. 바꾸면 둘을 같이 고친다.

    pending·EXPIRED 이고, 명령이 안 끝났고, 아직 유효(sent_at + dur 전)하고, 시도 수 < 상한이고,
    마지막 발송 뒤 RETRY_MIN 이 지났고, 그 단말에 더 새 명령이 없으면(superseded=False) 다시 보낸다.
    """
    if status not in RETRYABLE_STATUSES or cmd_finished or superseded:
        return False
    if attempts >= max_attempts:
        return False
    if now >= command_valid_until(cmd_sent_at, cmd_dur, timeout_sec):
        return False
    return not (
        last_sent_at is not None and now - last_sent_at < dt.timedelta(seconds=retry_min_sec)
    )


# ── 종료 판정 (30초 타이머) ───────────────────────────────────────────────
def finish_result(
    *, counts: dict[str, int], expired_exhausted: int, total: int, elapsed_sec: float,
    timeout_sec: int,
) -> str | None:
    """명령 결과. 아직 진행 중이면 None.

    counts = {status: n} (pending 포함). expired_exhausted = EXPIRED 이면서 시도를 다 쓴 수.
    종결 = OK/LOCAL/BAD/STATE + 시도 소진 EXPIRED. LOCAL("받았지만 현장 조작 중")도 종결이다.
      · 전부 종결          → 전부 OK 면 OK, 아니면 PARTIAL
      · COMMAND_TIMEOUT 경과 → 응답 0(전부 pending) 이면 TIMEOUT, 아니면 PARTIAL
    """
    ok = counts.get(TargetStatus.OK.value, 0)
    terminal = sum(counts.get(s, 0) for s in TERMINAL_STATUSES) + expired_exhausted
    if total > 0 and terminal >= total:
        return "OK" if ok >= total else "PARTIAL"
    if elapsed_sec >= timeout_sec:
        responded = total - counts.get(TargetStatus.PENDING.value, 0)
        if total == 0 or responded <= 0:
            return "TIMEOUT"
        return "PARTIAL"
    return None


# ── override 표시 (§3.10.8, S-19) ────────────────────────────────────────
@dataclass(frozen=True)
class OverrideState:
    act: str | None = None
    level: str | None = None
    seq: int | None = None
    until: dt.datetime | None = None


CLEARED = OverrideState()


#: ch 가 빠진 명령 = 모든 채널(§3.10.7).
ALL_CHANNELS = (1, 2, 3)


def _until_of(entry: dict[str, Any]) -> dt.datetime | None:
    raw = entry.get("until")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def channels_after_ok(
    current: dict[str, Any] | None, *, act: str, level: str, seq: int, sent_at: dt.datetime,
    dur: int | None, ch: list[int] | tuple[int, ...] | None, now: dt.datetime,
) -> dict[str, Any] | None:
    """OK 응답 뒤 device.override_ch 새 값. 바꿀 것이 없으면 None(빈 dict = 전부 해제).

    F/W 2026-09-27-9(§3.10.8 개정) — **채널마다 마지막에 받은 명령 하나**. 개별·그룹·전체 경로 사이
    우선순위는 없다(level 은 기록용). 유지시간이 끝나면 그 채널은 스케줄(이전 명령을 되살리지 않는다).
      · 명령의 ch 채널마다: 기록된 것이 **더 새 seq** 면 그대로(늦게 온 옛 재시도 OK 가 덮지 않게),
        아니면 auto → 그 채널 지움, on/off/pwm → {act, level, seq, until = 보낸 시각 + dur}.
      · 다른 채널은 건드리지 않는다. 이미 끝난 채널 기록은 이참에 버린다.
    """
    before = dict(current or {})
    m = {k: v for k, v in before.items()
         if isinstance(v, dict) and (u := _until_of(v)) is not None and u > now}
    for c in (ch or ALL_CHANNELS):
        key = str(c)
        cur = m.get(key)
        if cur is not None and isinstance(cur.get("seq"), int) and cur["seq"] > seq:
            continue
        if act == "auto":
            m.pop(key, None)
            continue
        if dur is None:
            continue
        m[key] = {"act": act, "level": level, "seq": seq,
                  "until": (sent_at + dt.timedelta(seconds=dur)).isoformat()}
    return None if m == before else m


def override_summary(channels: dict[str, Any] | None, now: dt.datetime) -> OverrideState:
    """override_ch → 요약 override_*(목록 필터·"원격 n분 남음"). 살아 있는 채널 중 **가장 늦게 끝나는** 것."""
    best: tuple[dt.datetime, dict[str, Any]] | None = None
    for v in (channels or {}).values():
        if not isinstance(v, dict):
            continue
        u = _until_of(v)
        if u is None or u <= now:
            continue
        if best is None or u > best[0]:
            best = (u, v)
    if best is None:
        return CLEARED
    v = best[1]
    return OverrideState(act=v.get("act"), level=v.get("level"), seq=v.get("seq"), until=best[0])


def channel_remaining(channels: dict[str, Any] | None, now: dt.datetime) -> dict[str, dict[str, Any]]:
    """화면용 {채널: {act, seq, level, remaining_sec}} — 끝난 채널은 뺀다."""
    out: dict[str, dict[str, Any]] = {}
    for k, v in (channels or {}).items():
        if not isinstance(v, dict):
            continue
        u = _until_of(v)
        if u is None or u <= now:
            continue
        out[k] = {"act": v.get("act"), "seq": v.get("seq"), "level": v.get("level"),
                  "remaining_sec": int((u - now).total_seconds())}
    return out


def remote_status(
    last_telemetry: dict[str, Any] | None, override_until: dt.datetime | None, now: dt.datetime
) -> tuple[bool, int | None]:
    """(remote_active, remote_remaining_sec). docs/05: active = md==2 AND until > now.

    남은 초는 until 이 미래면 md 와 무관하게 준다(OK 직후 아직 md 를 싣은 TM 이 안 왔을 때도
    화면이 "n분 남음 · 확인 대기"를 그릴 수 있게). 지났으면 None.
    """
    remaining = None
    if override_until is not None and override_until > now:
        remaining = int((override_until - now).total_seconds())
    md = (last_telemetry or {}).get("md")
    try:
        md_value = int(md) if md is not None and not isinstance(md, bool) else None
    except (TypeError, ValueError):
        md_value = None
    return (md_value == 2 and remaining is not None), remaining
