"""브로커 계정 파일 — go-auth `files` 백엔드용 passwd/aclfile 생성과 내보내기 (ADR-003).

2026-09-26 부터 passwd 에는 **서버 계정 `server`**(+ 1차 공용 `solarlte-test`, 켜 둔 동안만)만
들어간다. 단말은 파일에 없다 — username=UUID, password=HMAC 계산값이고 브로커(go-auth
`http` 백엔드)가 접속 순간 `/internal/mqtt/auth` 로 물어본다(app/modules/mqtt_auth).
CSV import 경로는 폐기했다.

해시 형식은 go-auth 의 `pw` 도구 기본값: `PBKDF2$sha512$100000$<salt b64>$<hash b64>`
(salt 16바이트, dklen 64). mosquitto_passwd 의 `$7$101$…` 과 다르다 — 브로커 이미지가
`iegomez/mosquitto-go-auth` 로 바뀌었기 때문이다.

salt 는 **결정적**이다: `HMAC-SHA256(key=username, msg=password)[:16]`. go-auth 는 SIGHUP 으로
passwd 를 다시 읽지 못해 entrypoint 감시 루프가 "파일 md5 가 바뀔 때만" 브로커를 재시작한다.
salt 가 기동마다 달라지면 내용이 같은데도 md5 가 바뀌어 5분마다 불필요한 재시작·로그가 난다
(인프라 실측 2026-09-26). 같은 (username, password) → 항상 같은 줄.

aclfile 도 go-auth files 형식이다. `user server` + `topic readwrite iotlight/#` 뿐이고
**pattern 줄은 없다** — 단말 ACL 은 `/internal/mqtt/acl` 이 답한다.

브로커 설치는 파일 경유다: 백엔드가 공유 볼륨에 떨어뜨리면 mosquitto 컨테이너의 entrypoint
감시 루프(infra/)가 설치하고 SIGHUP 으로 리로드한 뒤 `passwd.applied`/`aclfile.applied` 에
적용본 md5 를 적는다. 경로(MOSQUITTO_PASSWD_EXPORT / MOSQUITTO_ACL_EXPORT)가 비어 있으면
전부 no-op — 개발 PC 는 anonymous 브로커로 시험한다.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

# go-auth `pw` 기본값 (hasher pbkdf2, sha512, 100000 iterations, salt 16, keylen 64, base64).
GOAUTH_ALGO = "sha512"
GOAUTH_ITERATIONS = 100_000
_SALT_BYTES = 16
_KEY_LEN = 64
#: 적용 보고 파일 이름. 내보내기 경로와 같은 디렉터리.
ACL_APPLIED_MARKER = "aclfile.applied"
#: passwd 적용 보고. ACL 이 안 바뀐 내보내기는 aclfile.applied 가 즉시 일치하는데
#: passwd 설치는 감시 루프의 다음 1초라, 이것까지 봐야 "지금 접속 가능"이 된다(S2-06).
PASSWD_APPLIED_MARKER = "passwd.applied"


# ── 해시 ────────────────────────────────────────────────────────────────
def derive_salt(username: str, password: str) -> bytes:
    """(username, password) 에서 유도한 16바이트 salt. 같은 입력 → 같은 salt."""
    digest = hmac.new(username.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).digest()
    return digest[:_SALT_BYTES]


def goauth_hash(username: str, password: str, salt: bytes | None = None) -> str:
    """`PBKDF2$sha512$100000$<salt b64>$<hash b64>` — go-auth files 백엔드 형식.

    salt 를 안 주면 derive_salt() 로 결정적으로 만든다(모듈 docstring). 테스트가 다른 값을
    넣어 볼 때만 salt 를 준다.
    """
    salt = derive_salt(username, password) if salt is None else salt
    dk = hashlib.pbkdf2_hmac(
        GOAUTH_ALGO, password.encode("utf-8"), salt, GOAUTH_ITERATIONS, dklen=_KEY_LEN
    )
    return (
        f"PBKDF2${GOAUTH_ALGO}${GOAUTH_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(dk).decode('ascii')}"
    )


def verify_goauth_hash(password: str, stored: str) -> bool:
    """저장된 해시와 대조. 테스트·진단용 — 서버 계정 인증 경로는 브로커가 담당한다."""
    try:
        tag, algo, iterations, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if tag != "PBKDF2" or algo not in ("sha512", "sha256"):
        return False
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(
        algo, password.encode("utf-8"), salt, int(iterations), dklen=len(expected)
    )
    return dk == expected


def content_digest(content: str) -> str:
    """entrypoint 의 `md5sum` 과 같은 값."""
    return hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()


# ── 렌더 ────────────────────────────────────────────────────────────────
def render_passwd(
    *,
    server_username: str | None = None,
    server_password: str | None = None,
    test_enabled: bool | None = None,
    test_username: str | None = None,
    test_password: str | None = None,
) -> str:
    """passwd 파일 전체 내용 — 서버 계정 + (켜 둔 동안) 공용 시험 계정. 항상 통째로 재생성.

    키워드 인자는 테스트용 오버라이드다. 운영 경로는 settings 를 그대로 쓴다.
    """
    server_username = settings.mqtt_username if server_username is None else server_username
    server_password = settings.mqtt_password if server_password is None else server_password
    test_enabled = settings.mqtt_test_account_enabled if test_enabled is None else test_enabled
    test_username = settings.mqtt_test_username if test_username is None else test_username
    test_password = settings.mqtt_test_password if test_password is None else test_password

    lines = [
        "# 자동 생성 파일 — 손대지 말 것. 백엔드가 기동 때·5분마다 통째로 다시 만든다",
        "# (app/core/mqtt_accounts.py). go-auth files 백엔드 형식. 단말 계정은 여기 없다(HMAC).",
    ]
    if server_username and server_password:
        lines.append(f"{server_username}:{goauth_hash(server_username, server_password)}")
    else:
        log.warning("MQTT_PASSWORD 미설정 — 서버 계정 없이 passwd 를 만든다 (백엔드가 접속 불가)")

    # 1차 공용 계정. 전환 순서 5(사양서 §1.1.2.2)에서 MQTT_TEST_ACCOUNT_ENABLED=false 로 끈다.
    if test_enabled and test_username and test_password:
        lines.append(f"{test_username}:{goauth_hash(test_username, test_password)}")
    return "\n".join(lines) + "\n"


def render_acl(
    *,
    root: str | None = None,
    server_username: str | None = None,
    test_enabled: bool | None = None,
    test_username: str | None = None,
) -> str:
    """aclfile 전체 내용 — 서버 계정(+공용 시험 계정) 의 `iotlight/#` readwrite 뿐.

    pattern 줄을 넣지 않는다. 단말 ACL 은 go-auth http 백엔드가 `/internal/mqtt/acl` 로
    묻고, 그 답은 app/core/mqtt_acl.py 가 사양서 표대로 낸다.
    """
    root = settings.mqtt_topic_root if root is None else root
    server_username = settings.mqtt_username if server_username is None else server_username
    test_enabled = settings.mqtt_test_account_enabled if test_enabled is None else test_enabled
    test_username = settings.mqtt_test_username if test_username is None else test_username

    lines = [
        "# 자동 생성 파일 — 손대지 말 것. 백엔드가 기동 때·5분마다 통째로 다시 만든다",
        "# (app/core/mqtt_accounts.py). 단말 ACL 은 여기 없다 — /internal/mqtt/acl 이 답한다.",
        "",
        "# 서버 계정 — cmd/config 발행은 여기서만 한다.",
        f"user {server_username}",
        f"topic readwrite {root}/#",
        "",
    ]
    if test_enabled:
        lines += [
            "# 1차 공용 계정 (MQTT_TEST_ACCOUNT_ENABLED=true 일 때만). 단말 HMAC 전환 뒤 제거.",
            f"user {test_username}",
            f"topic readwrite {root}/#",
            "",
        ]
    return "\n".join(lines) + "\n"


# ── 내보내기 ────────────────────────────────────────────────────────────
@dataclass
class ExportResult:
    enabled: bool
    passwd_ok: bool = False
    acl_ok: bool = False
    passwd_md5: str | None = None
    acl_md5: str | None = None


def _export_file(target: Path, content: str, label: str) -> bool:
    """공유 볼륨에 원자적으로 쓴다. 실패는 로그만.

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
        log.exception("mosquitto %s 내보내기 실패 (%s) — 다음 내보내기 때 재시도", label, target)
        return False


def export_all() -> ExportResult:
    """passwd + aclfile 을 한 번에 내보낸다. 둘 중 하나만 갱신되는 상태를 만들지 않는다."""
    passwd_path = settings.mosquitto_passwd_export
    acl_path = settings.mosquitto_acl_export
    if not passwd_path or not acl_path:
        return ExportResult(enabled=False)

    passwd = render_passwd()
    acl = render_acl()
    result = ExportResult(
        enabled=True, passwd_md5=content_digest(passwd), acl_md5=content_digest(acl)
    )
    result.passwd_ok = _export_file(Path(passwd_path), passwd, "passwd(server 계정)")
    result.acl_ok = _export_file(Path(acl_path), acl, "aclfile")
    return result


def acl_applied_path() -> Path | None:
    """감시 루프가 "브로커가 지금 쓰는 aclfile 의 md5"를 적어 두는 파일."""
    acl_path = settings.mosquitto_acl_export
    return Path(acl_path).with_name(ACL_APPLIED_MARKER) if acl_path else None


def passwd_applied_path() -> Path | None:
    passwd_path = settings.mosquitto_passwd_export
    return Path(passwd_path).with_name(PASSWD_APPLIED_MARKER) if passwd_path else None


def _read_marker(marker: Path | None) -> str | None:
    if marker is None or not marker.exists():
        return None
    try:
        return marker.read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def read_applied_md5() -> str | None:
    return _read_marker(acl_applied_path())


def read_passwd_applied_md5() -> str | None:
    return _read_marker(passwd_applied_path())


async def wait_applied(
    passwd_md5: str | None, acl_md5: str | None, timeout: float | None = None
) -> bool:
    """passwd 와 aclfile 둘 다 브로커에 적용될 때까지 기다린다.

    passwd 보고 파일이 없는 감시 루프(옛 entrypoint)면 ACL 만 본다. 시간 안에 안 오면 False.
    """
    timeout = settings.acl_apply_timeout_sec if timeout is None else timeout
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    pw_marker = passwd_applied_path()
    check_pw = pw_marker is not None and pw_marker.exists() and passwd_md5 is not None
    while True:
        acl_ok = read_applied_md5() == acl_md5
        pw_ok = (not check_pw) or read_passwd_applied_md5() == passwd_md5
        if acl_ok and pw_ok:
            return True
        if loop.time() >= deadline:
            log.warning("브로커 계정 적용 확인 %.1f초 초과 (acl=%s passwd=%s)",
                        timeout, acl_ok, pw_ok)
            return False
        await asyncio.sleep(0.1)


async def wait_acl_applied(
    expected_md5: str | None,
    timeout: float | None = None,
    interval: float = 0.1,
) -> bool:
    """내보낸 aclfile 을 브로커가 읽어 들일 때까지 기다린다. 적용되면 True.

    적용 보고 파일이 아예 없으면(보고를 안 하는 감시 루프·개발 환경) 기다리지 않는다.
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
