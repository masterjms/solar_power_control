"""단말 ACL — 사양서 §1.1.2.2 ACL 표를 코드로 (ADR-003).

go-auth 는 `pattern %u` 파일 대신 `/internal/mqtt/acl` 에 (username, topic, acc) 를 물어본다.
표 그대로:

    write            iotlight/device/<u>/{register,status,result,event}
    read/subscribe   iotlight/device/<u>/{cmd,config}
    read/subscribe   iotlight/group/#   (하위 어떤 것이든 — 실행은 단말이 자기 grp 로 거른다)
    read/subscribe   iotlight/all/cmd

그 외는 전부 거부. 특히 와일드카드 구독(`iotlight/#`, `iotlight/device/+/status`)은 다른
단말의 메시지를 엿보는 경로라 거부한다 — 이것이 있어야 승인 게이트가 보안 기능이 된다.

`acc` 는 go-auth 규약: 1 read, 2 write, 3 readwrite, 4 subscribe. subscribe(4) 는 read 와 같이
본다(구독 허용 = 읽기 허용). readwrite(3) 는 read 와 write 둘 다 허용돼야 한다 — 단말
topic 에는 그런 것이 없으므로 항상 거부된다.

순수 함수. settings 를 읽지 않는다(root 는 호출부가 넘긴다).
"""

from __future__ import annotations

from app.constants import UUID_RE

ACC_READ = 1
ACC_WRITE = 2
ACC_READWRITE = 3
ACC_SUBSCRIBE = 4

_DEVICE_WRITE = ("register", "status", "result", "event")
_DEVICE_READ = ("cmd", "config")


def _has_wildcard(segment: str) -> bool:
    return "#" in segment or "+" in segment


def device_acl(username: str, topic: str, acc: int, *, root: str = "iotlight") -> bool:
    """이 단말(username=UUID)이 topic 에 acc 로 접근해도 되는가."""
    if not UUID_RE.match(username or ""):
        return False
    if acc not in (ACC_READ, ACC_WRITE, ACC_SUBSCRIBE):
        # readwrite(3) 나 모르는 값. 단말에 readwrite 로 열린 topic 은 없다.
        return False
    parts = topic.split("/")
    if len(parts) < 2 or parts[0] != root:
        return False

    want_read = acc in (ACC_READ, ACC_SUBSCRIBE)

    # iotlight/all/cmd — 읽기만
    if parts[1] == "all":
        return want_read and parts[2:] == ["cmd"]

    # iotlight/group/# — 하위 어떤 것이든 읽기. 와일드카드 구독도 허용(사양서 pattern read group/#).
    if parts[1] == "group":
        return want_read and len(parts) >= 3

    # iotlight/device/<u>/<kind> — 정확히 4단, 자기 UUID, 와일드카드 없음
    if parts[1] == "device":
        if len(parts) != 4 or parts[2] != username:
            return False
        kind = parts[3]
        if _has_wildcard(kind):
            return False
        if want_read:
            return kind in _DEVICE_READ
        return kind in _DEVICE_WRITE

    return False
