"""HMAC 비밀번호 — 사양서 §1.1.2.2 시험값이 그대로 나와야 한다."""

from __future__ import annotations

import pytest

from app.core.device_password import SPEC_TEST_KEY_HEX, device_password, parse_keys, verify

KEY = bytes.fromhex(SPEC_TEST_KEY_HEX)
UUID_A = "20363930594D50170004003A"
UUID_B = "00112233445566778899AABB"
PW_A = "70e8a87fba4a997f7c24d1f98300753a"
PW_B = "13f271bab5a9de23c3577ced778a46b9"


def test_spec_vectors():
    assert device_password(KEY, UUID_A) == PW_A
    assert device_password(KEY, UUID_B) == PW_B
    assert len(PW_A) == 32 and PW_A.lower() == PW_A


def test_uuid_is_upper_folded():
    assert device_password(KEY, UUID_A.lower()) == PW_A


def test_parse_keys_single_and_rotation():
    keys = parse_keys(f"K1:{SPEC_TEST_KEY_HEX}")
    assert list(keys) == ["K1"] and keys["K1"] == KEY
    keys = parse_keys(f" K1:{SPEC_TEST_KEY_HEX} , K2:{'ff' * 32} ,")
    assert list(keys) == ["K1", "K2"] and keys["K2"] == b"\xff" * 32
    assert parse_keys("") == {}


@pytest.mark.parametrize("raw", [
    "K1",                                   # 구분자 없음
    f"bad id:{SPEC_TEST_KEY_HEX}",          # ID 형식
    "K1:zz",                                # hex 아님
    "K1:0011",                              # 32B 아님
    f"K1:{SPEC_TEST_KEY_HEX},K1:{SPEC_TEST_KEY_HEX}",  # 중복
])
def test_parse_keys_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        parse_keys(raw)


def test_verify_returns_matching_key_id():
    keys = {"OLD": b"\x01" * 32, "TEST": KEY}
    assert verify(UUID_A, PW_A, keys) == "TEST"
    assert verify(UUID_B, PW_B, keys) == "TEST"
    assert verify(UUID_A, device_password(b"\x01" * 32, UUID_A), keys) == "OLD"


def test_verify_rejects():
    keys = {"TEST": KEY}
    assert verify(UUID_A, PW_A[:-1] + "b", keys) is None       # 한 글자 틀림
    assert verify(UUID_A, PW_B, keys) is None                  # 다른 UUID 의 값
    assert verify(UUID_A, "", keys) is None
    assert verify("server", PW_A, keys) is None                # UUID 형식 아님
    assert verify(UUID_A.lower(), PW_A, keys) is None          # 소문자 UUID 는 형식 위반
    assert verify(UUID_A, PW_A, {}) is None                    # 키 없음
