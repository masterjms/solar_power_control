#!/bin/sh
# iotlight mosquitto 기동 래퍼.
#
# 하는 일 세 가지:
#   A. 효력 conf 조립 — mosquitto.conf (+ 인증서가 있으면 mosquitto-tls.conf) 를
#      /mosquitto/data/mosquitto.effective.conf 로 이어붙여 그걸로 기동한다.
#      mosquitto 에는 조건부 include 가 없고, certfile 이 없는 8883 리스너는 기동 실패라
#      1차~3차(인증서 없음)와 4차(있음)를 같은 이미지·같은 conf 로 돌리기 위한 장치다.
#   B. passwd / aclfile 부트스트랩 — 공유 볼륨(/mosquitto/dynamic)의 *.generated 가
#      있으면 그것, 없으면 설치본, 그것도 없으면 시드(/mosquitto/config/*)로 시작.
#   C. 감시 루프 — 백엔드는 이 컨테이너에 신호를 못 보낸다(도커 소켓을 안 물린다).
#      대신 passwd.generated / aclfile.generated 를 볼륨에 떨어뜨리고, 이 루프가
#        1) 소유·권한을 mosquitto 0600 으로 맞춰 /mosquitto/data/ 로 설치
#        2) SIGHUP 리로드 (mosquitto 는 exec 되어 PID 1)
#        3) 지금 적용된 aclfile 의 md5 를 aclfile.applied 에 적는다
#      3) 은 백엔드가 "ACL 먼저, CONFIG 는 그다음"을 지키려고 읽는다. mosquitto 는 권한 없는
#      구독도 받아 두고 메시지를 넘길 때 ACL 을 보므로(aircast 2026-09-15 실측), 계정이
#      설치되기 전에 CONFIG_SET 이 가면 단말은 답을 못 받는다.
#
# 변경 감지는 내용(md5)이다. stat mtime 은 초 단위라 같은 초에 두 번 쓰면 두 번째 내용이
# 다음 변경 때까지 설치되지 않았다(aircast 에서 겪음).
#
# 백엔드 컨테이너(uid 10001)가 볼륨에 쓸 수 있도록 기동 때 소유자를 넘겨준다.
set -eu

DYN=/mosquitto/dynamic
DATA=/mosquitto/data
SEED=/mosquitto/config
CERTS=/mosquitto/certs
EFFECTIVE="$DATA/mosquitto.effective.conf"

digest() {  # 파일 내용 md5, 없으면 none
    if [ -f "$1" ]; then md5sum "$1" | cut -d' ' -f1; else echo none; fi
}

# 설치: 같은 파일시스템 안에서 임시 파일 → 원자적 교체. 리로드 순간에 반쯤 쓴
# 파일이 읽히는 일이 없게 한다.
install_file() {  # $1=원본, $2=설치 위치
    cp "$1" "$2.tmp"
    chown mosquitto:mosquitto "$2.tmp"
    chmod 600 "$2.tmp"
    mv "$2.tmp" "$2"
}

# 원본을 먼저 복사해 둔 사본의 md5 로 판정한다 — 복사 도중 백엔드가 파일을 바꿔도
# "설치한 내용"과 "보고하는 md5"가 어긋나지 않는다. 바뀌었으면 설치하고 md5 를 출력.
install_if_changed() {  # $1=generated 원본, $2=설치 위치, $3=지금 설치본 md5
    [ -f "$1" ] || return 1
    cp "$1" "$2.new"
    cur="$(digest "$2.new")"
    if [ "$cur" = "$3" ]; then
        rm -f "$2.new"
        return 1
    fi
    chown mosquitto:mosquitto "$2.new"
    chmod 600 "$2.new"
    mv "$2.new" "$2"
    echo "$cur"
}

report_acl() {  # $1=적용된 aclfile md5
    echo "$1" > "$DYN/aclfile.applied.tmp"
    mv "$DYN/aclfile.applied.tmp" "$DYN/aclfile.applied"
}

# passwd 도 같은 보고를 한다. ACL 내용이 안 바뀐 import(계정 추가만)는 aclfile.applied 가
# 즉시 일치해 백엔드가 "적용됨"으로 답하는데, passwd 설치는 이 루프의 다음 1초에 되므로
# 그 사이 단말 접속이 not authorised 로 거절된다(S2-06 에서 실제 발생). 백엔드는 둘 다 본다.
report_passwd() {  # $1=적용된 passwd md5
    echo "$1" > "$DYN/passwd.applied.tmp"
    mv "$DYN/passwd.applied.tmp" "$DYN/passwd.applied"
}

# 부트스트랩: generated 가 있으면 그것, 없고 설치본도 없으면 시드(리포 파일)를 쓴다.
bootstrap() {  # $1=이름(passwd|aclfile)
    if [ -f "$DYN/$1.generated" ]; then
        install_file "$DYN/$1.generated" "$DATA/$1"
    elif [ ! -f "$DATA/$1" ]; then
        if [ -f "$SEED/$1" ]; then
            cp "$SEED/$1" "$DATA/$1"
        else
            : > "$DATA/$1"
        fi
        chown mosquitto:mosquitto "$DATA/$1"
        chmod 600 "$DATA/$1"
    fi
}

# ── A. 효력 conf 조립 ────────────────────────────────────────────────
assemble_conf() {
    {
        echo "# 자동 생성 — entrypoint.sh 가 기동 때마다 다시 만든다. 손으로 고치지 말 것."
        echo "# 원본: /mosquitto/config/mosquitto.conf (+ mosquitto-tls.conf)"
        cat "$SEED/mosquitto.conf"
        if [ -f "$CERTS/server.crt" ] && [ -f "$CERTS/server.key" ]; then
            echo
            echo "# ── TLS 8883 (인증서 발견: $CERTS/server.crt) ──"
            cat "$SEED/mosquitto-tls.conf"
            echo "TLS 리스너 8883 활성 ($CERTS/server.crt)" >&2
        else
            echo "TLS 인증서 없음 — 8883 리스너를 만들지 않는다 (1차~3차 정상). 4차: docs/04_인프라_운영.md" >&2
        fi
    } > "$EFFECTIVE.tmp"
    mv "$EFFECTIVE.tmp" "$EFFECTIVE"
    chown mosquitto:mosquitto "$EFFECTIVE"
    chmod 644 "$EFFECTIVE"
}

mkdir -p "$DYN" "$DATA"
chown 10001 "$DYN" || true   # 백엔드 컨테이너 사용자(uid 10001)

assemble_conf

# ── B. 부트스트랩 ────────────────────────────────────────────────────
bootstrap passwd
bootstrap aclfile

# ── C. 감시 루프 ─────────────────────────────────────────────────────
(
    last_pw="$(digest "$DATA/passwd")"
    last_acl="$(digest "$DATA/aclfile")"
    # mosquitto 가 기동하며 이 aclfile 을 읽는다. 기동 직후부터 적용 보고가 있어야 백엔드가
    # 기다릴 기준을 안다.
    report_acl "$last_acl"
    report_passwd "$last_pw"
    while :; do
        sleep 1
        changed=0
        pw_changed=0
        if cur="$(install_if_changed "$DYN/passwd.generated" "$DATA/passwd" "$last_pw")"; then
            last_pw="$cur"; changed=1; pw_changed=1
        fi
        acl_changed=0
        if cur="$(install_if_changed "$DYN/aclfile.generated" "$DATA/aclfile" "$last_acl")"; then
            last_acl="$cur"; changed=1; acl_changed=1
        fi
        if [ "$changed" = 1 ]; then
            kill -HUP 1
            # 보고는 HUP 뒤에 한다 — 백엔드가 보고를 보는 순간 브로커는 이미 새 파일을 읽었다.
            [ "$acl_changed" = 1 ] && report_acl "$last_acl"
            [ "$pw_changed" = 1 ] && report_passwd "$last_pw"
            echo "passwd/aclfile 갱신 설치 + 리로드 완료"
        fi
    done
) &

# 원본 이미지 entrypoint 를 거쳐 mosquitto 를 PID 1 로 exec 한다.
exec /docker-entrypoint.sh mosquitto -c "$EFFECTIVE"
