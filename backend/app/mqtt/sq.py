"""`sq` 판정 — 유실과 재부팅을 함께 본다 (사양서 §1.1.6).

    번호가 건너뜀   → 그만큼 유실 (전송 실패해도 단말은 sq 를 올린다)
    번호가 되돌아감 → 그 사이 재부팅 (sq 는 전원 인가 시 0 부터)

uint32 wrap: 단말이 32-bit 카운터를 쓰는지는 확인 요청 중(docs/00 §7). 확인 전까지는
"직전 값이 상한 근처였고 새 값이 0 근처"인 경우만 wrap 으로 보고 재부팅으로 세지 않는다.
10분 주기로 42억 번은 8만 년이라 실제로는 안 일어나지만, 판정이 재부팅 카운트를 건드리는
만큼 규칙을 명시해 둔다.

순수 함수다. DB 도 시계도 모른다.
"""

from __future__ import annotations

from dataclasses import dataclass

UINT32_MAX = 4_294_967_295
#: 이 안쪽에서 되돌아가면 wrap 으로 본다.
WRAP_MARGIN = 1_000
#: 한 번에 유실로 세는 상한. 10분 주기 기준 약 2년치다. 이보다 큰 점프는 유실이 아니라
#: 카운터 이상(펌웨어 교체·시험 도구의 sq 강제 점프)으로 보고, lost_count 는 상한까지만 더한다.
#: 상한이 없으면 sq 가 0 근처에서 2^32 근처로 뛰었을 때 lost_count(int4) 가 넘쳐 flush 전체가
#: 실패하고, 같은 배치의 다른 단말 TM 까지 버려진다(S2-03 에서 실제 발생).
LOST_CAP = 100_000


@dataclass(frozen=True)
class SqVerdict:
    lost: int = 0
    reboot: bool = False
    duplicate: bool = False
    #: 실제 건너뛴 칸 수(상한 적용 전). 이벤트 payload 에 원값으로 남긴다.
    jump: int = 0


def judge(last_sq: int | None, sq: int) -> SqVerdict:
    if last_sq is None:
        # 처음 보는 단말. 기준이 없으니 아무것도 세지 않는다.
        return SqVerdict()
    if sq == last_sq + 1:
        return SqVerdict()
    if sq == last_sq:
        # status 는 QoS0 이라 브로커 재전송은 없지만 단말이 같은 값을 두 번 보낼 수는 있다.
        return SqVerdict(duplicate=True)
    if sq > last_sq:
        gap = sq - last_sq - 1
        return SqVerdict(lost=min(gap, LOST_CAP), jump=gap)
    # sq < last_sq
    if last_sq > UINT32_MAX - WRAP_MARGIN and sq < WRAP_MARGIN:
        gap = (UINT32_MAX - last_sq) + sq
        return SqVerdict(lost=min(gap, LOST_CAP), jump=gap)
    return SqVerdict(reboot=True)
