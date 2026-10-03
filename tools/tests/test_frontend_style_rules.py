"""화면 글자 규칙(문제점 26번) — 크기는 style.css 의 토큰(--f1~--f4·--f-touch)만, 인라인 fontSize 금지, 글꼴은 --font/--mono.

프런트에 시험 도구가 없어 여기서 파일을 읽어 검사한다. 규칙 자체는 style.css 머리 주석과 docs/07 "글자 규칙".
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
CSS = SRC / "style.css"


def test_font_sizes_use_tokens_only():
    css = CSS.read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    raw = [m.group(0) for m in re.finditer(r"font-size\s*:\s*[^;}]+", body)
           if not re.search(r"var\(--f(?:[1-4]|-touch)\)", m.group(0))]
    assert raw == [], f"토큰이 아닌 글자 크기: {raw}"


def test_font_weights_are_400_or_600():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    bad = [m.group(0) for m in re.finditer(r"font-weight\s*:\s*([^;}]+)", css)
           if m.group(1).strip() not in ("400", "600")]
    assert bad == [], f"400·600 이 아닌 굵기: {bad}"


def test_no_inline_font_size_or_family_in_tsx():
    hits = []
    for f in SRC.glob("*.tsx"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bfontSize\b|\bfontFamily\b|\bfontWeight\b", line):
                hits.append(f"{f.name}:{i}")
    assert hits == [], f"인라인 글자 지정: {hits}"


def test_font_families_are_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    fams = [m.group(0) for m in re.finditer(r"font-family\s*:\s*[^;}]+", css)
            if "var(--font)" not in m.group(0) and "var(--mono)" not in m.group(0)]
    assert fams == [], f"토큰이 아닌 글꼴: {fams}"
