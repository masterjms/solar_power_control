from __future__ import annotations

from app.mqtt.sq import UINT32_MAX, SqVerdict, judge


def test_first_sample_counts_nothing():
    assert judge(None, 41) == SqVerdict()


def test_sequential():
    assert judge(41, 42) == SqVerdict()


def test_gap_is_lost():
    assert judge(41, 45) == SqVerdict(lost=3, jump=3)
    assert judge(0, 1) == SqVerdict()
    assert judge(0, 2) == SqVerdict(lost=1, jump=1)


def test_duplicate():
    assert judge(41, 41) == SqVerdict(duplicate=True)


def test_backwards_is_reboot():
    assert judge(41, 0) == SqVerdict(reboot=True)
    assert judge(41, 5) == SqVerdict(reboot=True)
    assert judge(500_000, 499_000) == SqVerdict(reboot=True)


def test_uint32_wrap_is_lost_not_reboot():
    assert judge(UINT32_MAX, 0) == SqVerdict(lost=0)
    assert judge(UINT32_MAX - 1, 0) == SqVerdict(lost=1, jump=1)
    assert judge(UINT32_MAX, 3) == SqVerdict(lost=3, jump=3)
    # 상한 근처가 아니면 되돌아감 = 재부팅
    assert judge(UINT32_MAX - 5_000, 0) == SqVerdict(reboot=True)


def test_huge_jump_is_capped():
    # 0 근처 → 2^32 근처 점프: 유실이 아니라 카운터 이상. lost 는 상한, jump 는 원값.
    from app.mqtt.sq import LOST_CAP
    v = judge(5, UINT32_MAX - 1)
    assert v.lost == LOST_CAP and v.jump == UINT32_MAX - 7 and not v.reboot
