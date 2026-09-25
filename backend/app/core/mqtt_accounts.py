"""단말별 MQTT 계정 — mosquitto passwd/aclfile 생성과 내보내기 (사양서 §1.1.2.2).

username = 단말 UUID 24자리(Client ID 와 같다), password = PC 설정 도구가 만든 무작위
문자열. 서버는 **해시만** 저장한다(docs/00 §4 "비밀번호 평문 저장 → 해시만").
평문은 PC 도구의 CSV 에만 있고, import 순간 해시로 바뀌어 DB 에 들어간다.

브로커 등록은 파일 경유다: 백엔드가 passwd/aclfile 을 공유 볼륨에 떨어뜨리면
mosquitto 컨테이너의 entrypoint 감시 루프(infra/)가 설치하고 SIGHUP 으로 리로드한 뒤
`aclfile.applied` 에 적용본 md5 를 적는다. 백엔드는 mosquitto 컨테이너에 직접 신호를
보낼 수 없어서(도커 소켓을 안 물린다) 이 간접 구조를 쓴다.

순서가 중요하다: **export → applied 확인 → CONFIG/명령 발행.** mosquitto 는 구독은
받아 두고 메시지를 넘길 때 ACL 을 보므로, 권한이 설치되기 전에 나간 메시지는 그
단말에 영영 안 간다(aircast 2026-09-15 실측).

경로(MOSQUITTO_PASSWD_EXPORT / MOSQUITTO_ACL_EXPORT)가 비어 있으면 전부 no-op —
개발 PC 는 anonymous 브로커로 시험한다.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import logging
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.constants import UUID_RE

log = logging.getLogger(__name__)

# mosquitto_passwd 의 sha512-pbkdf2 형식과 동일한 파라미터 (2.x 기본값).
# eclipse-mosquitto:2 인증 실측 통과 — aircast 2026-08-30.
HASH_ITERATIONS = 101
_SALT_BYTES = 12
#: 적용 보고 파일 이름. aclfile 내보내기 경로와 같은 디렉터리.
ACL_APPLIED_MARKER = "aclfile.applied"


# ── 해시 ────────────────────────────────────────────────────────────────
def mosquitto_hash(password: str, salt: bytes | None = None) -> str:
    """`$7$<iterations>$<salt b64>$<hash b64>` — mosquitto 의 PBKDF2-SHA512 형식.

    salt 는 테스트가 고정값을 넣을 때만 준다. 운영 경로는 항상 무작위다.
    """
    salt = os.urandom(_SALT_BYTES) if salt is None else salt
    dk = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, HASH_ITERATIONS, dklen=64)
    return (
        f"$7${HASH_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(dk).decode('ascii')}"
    )


def verify_mosquitto_hash(password: str, stored: str) -> bool:
    """저장된 해시와 대조. 테스트·진단용 — 서버 인증 경로는 브로커가 담당한다."""
    try:
        _, algo, iterations, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "7":
        return False
    salt = base64.b64decode(salt_b64)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, int(iterations), dklen=64)
    return base64.b64encode(dk).decode("ascii") == dk_b64


def content_digest(content: str) -> str:
    """entrypoint 의 `md5sum` 과 같은 값."""
    return hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()


# ── 렌더 ────────────────────────────────────────────────────────────────
def render_passwd(
    device_hashes: Mapping[str, str],
    *,
    server_username: str | None = None,
    server_password: str | None = None,
    test_enabled: bool | None = None,
    test_username: str | None = None,
    test_password: str | None = None,
) -> str:
    """passwd 파일 전체 내용. DB 가 정본이고 파일은 항상 통째로 재생성한다.

    부분 수정(기존 파일 읽어서 병합)을 안 하는 이유: 파일과 DB 가 어긋난 상태가
    조용히 굳는 것이 최악이라서다.

    키워드 인자는 테스트용 오버라이드다. 운영 경로는 settings 를 그대로 쓴다.
    """
    server_username = settings.mqtt_username if server_username is None else server_username
    server_password = settings.mqtt_password if server_password is None else server_password
    test_enabled = settings.mqtt_test_account_enabled if test_enabled is None else test_enabled
    test_username = settings.mqtt_test_username if test_username is None else test_username
    test_password = settings.mqtt_test_password if test_password is None else test_password

    lines = [
        "# 자동 생성 파일 — 손대지 말 것. 정본은 서버 DB 다.",
        "# 백엔드가 계정 import/삭제/기동 때마다 통째로 다시 만든다 (app/core/mqtt_accounts.py).",
    ]
    if server_username and server_password:
        lines.append(f"{server_username}:{mosquitto_hash(server_password)}")
    else:
        log.warning("MQTT_PASSWORD 미설정 — 서버 계정 없이 passwd 를 만든다 (백엔드가 접속 불가)")

    # 1차 공용 계정. 2차 합격 기준 1번(공용 계정 거부)을 만족하려면 꺼야 한다.
    if test_enabled and test_username and test_password:
        lines.append(f"{test_username}:{mosquitto_hash(test_password)}")

    for uuid in sorted(device_hashes):
        lines.append(f"{uuid}:{device_hashes[uuid]}")
    return "\n".join(lines) + "\n"


def render_acl(
    *,
    root: str | None = None,
    server_username: str | None = None,
    test_enabled: bool | None = None,
    test_username: str | None = None,
) -> str:
    """aclfile 전체 내용 — 사양서 §1.1.2.2 그대로.

    `%u` 가 username(=UUID)으로 치환되므로 단말마다 블록을 만들 필요가 없다. 단말 수가
    1만이어도 파일은 이 몇 줄이다. 5차 그룹 topic 은 `group/#` 읽기를 전 단말에 열고
    실행은 단말이 자기 grp 목록으로 거른다(사양서 결정).
    """
    root = settings.mqtt_topic_root if root is None else root
    server_username = settings.mqtt_username if server_username is None else server_username
    test_enabled = settings.mqtt_test_account_enabled if test_enabled is None else test_enabled
    test_username = settings.mqtt_test_username if test_username is None else test_username

    lines = [
        "# 자동 생성 파일 — 손대지 말 것. 정본은 서버 DB 다.",
        "# 백엔드가 계정 import/삭제/기동 때마다 통째로 다시 만든다 (app/core/mqtt_accounts.py).",
        "",
        "# 서버 계정 — cmd/config 발행은 여기서만 한다.",
        f"user {server_username}",
        f"topic readwrite {root}/#",
        "",
    ]
    if test_enabled:
        lines += [
            "# 1차 공용 계정 (MQTT_TEST_ACCOUNT_ENABLED=true 일 때만). 2차 합격 전 제거.",
            f"user {test_username}",
            f"topic readwrite {root}/#",
            "",
        ]
    lines += [
        "# 단말별 topic. %u = username = UUID 24자리 (비밀번호로 증명된 값).",
        f"pattern write {root}/device/%u/register",
        f"pattern write {root}/device/%u/status",
        f"pattern write {root}/device/%u/result",
        f"pattern write {root}/device/%u/event",
        f"pattern read  {root}/device/%u/cmd",
        f"pattern read  {root}/device/%u/config",
        f"pattern read  {root}/group/#",
        f"pattern read  {root}/all/cmd",
    ]
    return "\n".join(lines) + "\n"


# ── CSV import ──────────────────────────────────────────────────────────
@dataclass
class CsvParseResult:
    accounts: dict[str, str]
    errors: list[str]


def parse_accounts_csv(content: str) -> CsvParseResult:
    """`uuid,password` CSV → {uuid: 평문}. 헤더 행은 있어도 되고 없어도 된다.

    같은 uuid 가 두 번 나오면 뒤의 것이 이긴다(도구가 재발급한 경우). uuid 는 대문자로
    접는다 — 도구가 소문자로 뽑아도 topic/Client ID 는 대문자다.
    """
    accounts: dict[str, str] = {}
    errors: list[str] = []
    reader = csv.reader(io.StringIO(content))
    for lineno, row in enumerate(reader, start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < 2:
            errors.append(f"{lineno}행: 열이 2개가 아닙니다")
            continue
        uuid, password = row[0].strip().upper(), row[1].strip()
        if lineno == 1 and uuid.lower() == "uuid":
            continue  # 헤더
        if not UUID_RE.match(uuid):
            errors.append(f"{lineno}행: UUID 형식 아님 ({row[0].strip()!r})")
            continue
        if not password:
            errors.append(f"{lineno}행: 비밀번호가 비었습니다")
            continue
        if ":" in password or any(ch.isspace() for ch in password):
            # passwd 파일 구분자와 충돌하거나 mosquitto 가 잘라 읽는다.
            errors.append(f"{lineno}행: 비밀번호에 ':' 나 공백을 쓸 수 없습니다")
            continue
        accounts[uuid] = password
    return CsvParseResult(accounts=accounts, errors=errors)


# ── 내보내기 ────────────────────────────────────────────────────────────
@dataclass
class ExportResult:
    enabled: bool
    passwd_ok: bool = False
    acl_ok: bool = False
    passwd_md5: str | None = None
    acl_md5: str | None = None


def _export_file(target: Path, content: str, label: str) -> bool:
    """공유 볼륨에 원자적으로 쓴다. 실패는 로그만 — 정본은 DB 다.

    같은 디렉터리에 임시 파일 → os.replace. 감시 루프가 반쯤 쓴 파일을 설치하는 일이
    없게 한다. 바이트 그대로 쓴다(개행 변환 없음) — 감시 루프의 md5 와 같아야 한다.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content.encode("utf-8"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
        log.info("mosquitto %s 내보냄 → %s", label, target)
        return True
    except OSError:
        log.exception("mosquitto %s 내보내기 실패 (%s) — DB 는 정상, 다음 내보내기 때 재시도",
                      label, target)
        return False


def export_all(device_hashes: Mapping[str, str]) -> ExportResult:
    """passwd + aclfile 을 한 번에 내보낸다. 둘 중 하나만 갱신되는 상태를 만들지 않는다."""
    passwd_path = settings.mosquitto_passwd_export
    acl_path = settings.mosquitto_acl_export
    if not passwd_path or not acl_path:
        return ExportResult(enabled=False)

    passwd = render_passwd(device_hashes)
    acl = render_acl()
    result = ExportResult(
        enabled=True, passwd_md5=content_digest(passwd), acl_md5=content_digest(acl)
    )
    result.passwd_ok = _export_file(
        Path(passwd_path), passwd, f"passwd(단말 {len(device_hashes)}대)"
    )
    result.acl_ok = _export_file(Path(acl_path), acl, "aclfile")
    return result


def acl_applied_path() -> Path | None:
    """감시 루프가 "브로커가 지금 쓰는 aclfile 의 md5"를 적어 두는 파일."""
    acl_path = settings.mosquitto_acl_export
    return Path(acl_path).with_name(ACL_APPLIED_MARKER) if acl_path else None


def read_applied_md5() -> str | None:
    marker = acl_applied_path()
    if marker is None or not marker.exists():
        return None
    try:
        return marker.read_text(encoding="ascii").strip() or None
    except OSError:
        return None


async def wait_acl_applied(
    expected_md5: str | None,
    timeout: float | None = None,
    interval: float = 0.1,
) -> bool:
    """내보낸 aclfile 을 브로커가 읽어 들일 때까지 기다린다. 적용되면 True.

    적용 보고 파일이 아예 없으면(보고를 안 하는 감시 루프·개발 환경) 기다리지 않는다.
    시간 안에 안 오면 경고만 남기고 False — 요청을 실패시킬 일은 아니다. 호출부는
    응답에 `acl_applied` 로 실어 운영자가 알게 한다.
    """
    timeout = settings.acl_apply_timeout_sec if timeout is None else timeout
    marker = acl_applied_path()
    if marker is None or expected_md5 is None or not marker.exists():
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if read_applied_md5() == expected_md5:
            return True
        if loop.time() >= deadline:
            log.warning("브로커 aclfile 적용 확인 %.1f초 초과 (기대 md5=%s)", timeout, expected_md5)
            return False
        await asyncio.sleep(interval)


def uuids_of(rows: Iterable[tuple[str, str | None]]) -> dict[str, str]:
    """(uuid, hash) 행 → 해시가 있는 것만 dict."""
    return {uuid: h for uuid, h in rows if h}
