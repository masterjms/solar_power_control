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


@dataclass(frozen=True)
class SqVerdict:
    lost: int = 0
    reboot: bool = False
    duplicate: bool = False


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
        return SqVerdict(lost=sq - last_sq - 1)
    # sq < last_sq
    if last_sq > UINT32_MAX - WRAP_MARGIN and sq < WRAP_MARGIN:
        return SqVerdict(lost=(UINT32_MAX - last_sq) + sq)
    return SqVerdict(reboot=True)
