"""suntable 사본이 기준 구현과 같은지 — 시험값 118건 CRC 전부 일치 (docs/spec/ref/README.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.vendor import suntable

REF = Path(__file__).resolve().parents[2] / "docs" / "spec" / "ref"
VECTORS = REF / "suntable_vectors.json"
VENDOR = Path(suntable.__file__)


@pytest.mark.skipif(not VECTORS.exists(), reason="docs/spec/ref 없음(컨테이너 안)")
def test_all_vector_crcs_match():
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert data["version"] == suntable.VERSION
    cases = data["cases"]
    assert len(cases) == 118
    for case in cases:
        table = suntable.build_table(case["lat_e6"], case["lon_e6"], case["on"], case["off"])
        assert len(table) == suntable.DAY_SLOTS
        crc = f"{suntable.table_crc32(table):08X}"
        assert crc == case["crc32"].upper(), case["name"]
        for mmdd, expected in case.get("sample", {}).items():
            month, day = (int(x) for x in mmdd.split("-"))
            assert list(table[(month - 1) * 31 + day - 1]) == expected, (case["name"], mmdd)


@pytest.mark.skipif(not (REF / "suntable.py").exists(), reason="docs/spec/ref 없음")
def test_vendor_copy_is_identical():
    """고치지 말고 그대로 쓴다(README) — 사본이 원본과 같아야 한다. 줄끝(CRLF/LF)은 체크아웃
    환경(.gitattributes eol=lf, Windows 작업본)에 따라 달라지므로 그것만 맞춰 비교한다."""

    def norm(path: Path) -> list[bytes]:
        return path.read_bytes().splitlines()

    assert norm(VENDOR) == norm(REF / "suntable.py")
