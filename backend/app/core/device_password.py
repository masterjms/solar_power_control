"""단말 비밀번호 — HMAC-SHA256(K, UUID) 계산값 (사양서 §1.1.2.2, ADR-003).

비밀번호는 아무도 저장하지 않는다. 단말은 부팅 때 계산하고, 브로커(go-auth http 백엔드)는
접속 순간 `/internal/mqtt/auth` 로 물어보며, 여기서 같은 값을 계산해 비교한다.

    입력  : UUID 24자 (대문자 ASCII)
    키    : K 32바이트 (환경 변수 MQTT_HMAC_KEYS 에 "K1:<hex64>" — 로그·DB 에 남기지 않는다)
    출력  : HMAC-SHA256 앞 16바이트 → 소문자 hex 32자

활성 키가 여럿이면(교체 중 K1+K2) 전부 대조하고 하나라도 맞으면 통과한다. 비교는 항상
`hmac.compare_digest` — 키 개수와 무관하게 **모든** 키를 끝까지 대조해 타이밍으로 어느
키가 맞았는지 새지 않게 한다.

이 파일은 settings 를 읽지 않는다(순수 함수 + 파서). 키 목록은 호출부가 넘긴다.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping

from app.constants import UUID_RE

#: 사양서 §1.1.2.2 공개 시험 키(0x00~0x1F). 운영 키가 아니다 — .env.example 과 단위 시험용.
SPEC_TEST_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"

_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_KEY_BYTES = 32


def device_password(key: bytes, uuid: str) -> str:
    """사양서 §1.1.2.2 의 참조 구현 그대로. uuid 는 대문자로 접어 넣는다."""
    return hmac.new(key, uuid.upper().encode("ascii"), hashlib.sha256).hexdigest()[:32]


def parse_keys(raw: str) -> dict[str, bytes]:
    """`"K1:<hex64>,K2:<hex64>"` → {key_id: 32바이트}. 순서를 유지한다(dict).

    형식이 틀린 항목은 조용히 넘기지 않고 ValueError — 키가 잘못 들어가면 전 단말이 접속을
    못 하므로 기동 때 바로 죽는 편이 낫다. 빈 문자열은 빈 dict(인증 API 가 전부 403).
    """
    keys: dict[str, bytes] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key_id, sep, hex_value = item.partition(":")
        key_id, hex_value = key_id.strip(), hex_value.strip()
        if not sep or not _KEY_ID_RE.match(key_id):
            raise ValueError(
                f"MQTT_HMAC_KEYS 항목 형식 오류: 'ID:hex64' 이어야 합니다 ({item[:8]}…)"
            )
        try:
            key = bytes.fromhex(hex_value)
        except ValueError as exc:
            raise ValueError(f"MQTT_HMAC_KEYS[{key_id}] 가 hex 가 아닙니다") from exc
        if len(key) != _KEY_BYTES:
            raise ValueError(f"MQTT_HMAC_KEYS[{key_id}] 길이 {len(key)}B — 32B 이어야 합니다")
        if key_id in keys:
            raise ValueError(f"MQTT_HMAC_KEYS 키 ID 중복: {key_id}")
        keys[key_id] = key
    return keys


def verify(
    username: str, password: str, keys: Mapping[str, bytes] | Iterable[tuple[str, bytes]]
) -> str | None:
    """맞는 키의 ID 를 돌려준다. 없으면 None.

    username 이 UUID 형식이 아니면 계산도 하지 않는다(서버·공용 계정은 files 백엔드 몫).
    모든 키를 끝까지 대조한다 — 첫 일치에서 끊으면 어느 키인지 타이밍으로 샌다.
    """
    if not isinstance(username, str) or not UUID_RE.match(username):
        return None
    if not isinstance(password, str) or not password:
        return None
    items = keys.items() if isinstance(keys, Mapping) else keys
    matched: str | None = None
    for key_id, key in items:
        if hmac.compare_digest(device_password(key, username), password):
            matched = key_id
    return matched
