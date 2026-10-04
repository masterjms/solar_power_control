"""관리 화면 계정 — 최고관리자 / 지역관리자 / 게스트 (문제점 21번, ADR-013). 순수 규칙만.

역할
  super_admin   모든 기능. 최대 3명(.env 의 최고관리자 포함).
  region_admin  시·도 단위(트리 최상위 노드, 여러 개 가능) 안의 단말·지역만. 통신 주기 설정·서버 설정·
                서버 상태·계정 관리는 없다. 단말 삭제는 못 한다(폐기·중지·거절은 된다).
  guest         받은 시·도의 대시보드만 본다(읽기 전용). 버튼은 없다.
  admin         옛 .env OPERATOR 계정 — 전 지역, 최고관리자 아님(하위 호환). 화면에서 만들 수 없다.

계정은 DB(`admin_user`). .env 의 ADMIN_USER(최고관리자)·OPERATOR_USER 는 그대로 쓰되 화면에서 지우거나 바꿀 수 없다.
비밀번호는 PBKDF2-SHA256(표준 라이브러리, 반복 240,000) — 외부 패키지를 더하지 않는다.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import re
from collections.abc import Iterable

ROLE_SUPER = "super_admin"
ROLE_REGION = "region_admin"
ROLE_GUEST = "guest"
ROLE_ADMIN = "admin"  # 옛 .env OPERATOR(전 지역)

#: 화면에서 만들 수 있는 역할
CREATABLE_ROLES = (ROLE_SUPER, ROLE_REGION, ROLE_GUEST)
ROLE_LABEL = {ROLE_SUPER: "최고관리자", ROLE_REGION: "지역관리자", ROLE_GUEST: "게스트",
              ROLE_ADMIN: "관리자"}

#: 최고관리자 최대 수(.env 최고관리자 포함) — 단말측 결정(10/3).
MAX_SUPER = 3
PASSWORD_MIN = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")  # '.' 은 세션 쿠키 구분자라 쓰지 않는다

#: 사용 기간 고르기 — 단말측 결정(7·15·30·90·180일·1년·무한).
EXPIRY_DAYS = {"7d": 7, "15d": 15, "30d": 30, "90d": 90, "180d": 180, "365d": 365, "never": None}

_ITER = 240_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """'pbkdf2_sha256$반복$salt$hash' (base64)."""
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, it, salt_b64, dk_b64 = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(it))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def expires_from(choice: str, now: dt.datetime) -> dt.datetime | None:
    """사용 기간 선택 → 만료 시각(지금부터). never → None. 모르는 값은 ValueError."""
    if choice not in EXPIRY_DAYS:
        raise ValueError(choice)
    days = EXPIRY_DAYS[choice]
    return None if days is None else now + dt.timedelta(days=days)


def password_changed_now(now: dt.datetime) -> dt.datetime:
    """비밀번호를 바꾼 시각 — 다음 정수 초로 올린다. 로그인 쿠키는 초 단위라 같은 초에 받은 옛 쿠키를
    가려내려면 이래야 한다(바꾼 뒤 1초 안에 다시 로그인하는 일은 사람에게 없다)."""
    return now.replace(microsecond=0) + dt.timedelta(seconds=1)


def is_valid(*, disabled: bool, expires_at: dt.datetime | None, now: dt.datetime) -> bool:
    return not disabled and (expires_at is None or expires_at > now)


def check_new_account(
    *, username: str, password: str | None, role: str, region_ids: Iterable[int] | None,
    reserved: Iterable[str], top_ids: Iterable[int], super_count: int,
) -> dict[str, str]:
    """만들거나 바꿀 계정 검사. {필드: 이유}(비면 통과). password=None 이면 비밀번호는 안 본다.
    super_count = 이 계정을 빼고 지금 있는 최고관리자 수(.env 포함)."""
    errors: dict[str, str] = {}
    if not USERNAME_RE.match(username or ""):
        errors["username"] = "영문·숫자·_·- 3~32자"
    elif username in set(reserved):
        errors["username"] = "이미 있는 이름(.env 계정)"
    if password is not None and len(password) < PASSWORD_MIN:
        errors["password"] = f"{PASSWORD_MIN}자 이상"
    if role not in CREATABLE_ROLES:
        errors["role"] = "최고관리자·지역관리자·게스트 중 하나"
    elif role == ROLE_SUPER:
        if super_count >= MAX_SUPER:
            errors["role"] = f"최고관리자는 {MAX_SUPER}명까지(.env 계정 포함)"
    else:
        ids = list(region_ids or [])
        tops = set(top_ids)
        if not ids:
            errors["region_ids"] = "시·도를 하나 이상 고른다"
        elif any(i not in tops for i in ids):
            errors["region_ids"] = "시·도(트리 맨 위)만 고를 수 있다"
    return errors


def scope_node_ids(region_ids: Iterable[int] | None, subtree) -> list[int] | None:  # noqa: ANN001
    """시·도 id 목록 → 그 아래 전체 노드 id(자신 포함). None = 전 지역. subtree(id) -> list[int]."""
    if region_ids is None:
        return None
    out: list[int] = []
    for rid in region_ids:
        out.extend(subtree(rid) or [rid])
    return sorted(set(out))
