#!/bin/sh
# iotlight mosquitto 기동 래퍼 — 이미지 iegomez/mosquitto-go-auth (ADR-003).
#
# 하는 일 네 가지:
#   A. 효력 conf 조립 — mosquitto.conf (+ 인증서가 있으면 mosquitto-tls.conf) 를
#      /mosquitto/data/mosquitto.effective.conf 로 이어붙여 그걸로 기동한다.
#      mosquitto 에는 조건부 include 가 없고, certfile 이 없는 8883 리스너는 기동 실패라
#      1차~3차(인증서 없음)와 4차(있음)를 같은 이미지·같은 conf 로 돌리기 위한 장치다.
#   B. passwd / aclfile 부트스트랩 — 공유 볼륨(/mosquitto/dynamic)의 *.generated 가
#      있으면 그것, 없으면 설치본, 그것도 없으면 시드로 시작.
#      passwd 는 go-auth files 형식(user:PBKDF2$sha512$100000$<salt b64>$<hash b64>)이라
#      mosquitto_passwd 가 아니라 이미지의 /mosquitto/pw 로 만든다. 시드가 없으면
#      solarlte-test(1차 공용) 와, MQTT_PASSWORD 가 넘어왔으면 server 계정을 만들어 시작.
#      단말(UUID) 계정은 passwd 에 넣지 않는다 — 접속 때 백엔드가 HMAC 으로 판정한다.
#   C. 브로커 로그 파일 — mosquitto.conf 의 `log_dest file /mosquitto/dynamic/mosquitto.log`.
#      백엔드(uid 10001)가 같은 볼륨을 /var/lib/iotlight/mqtt 로 마운트해 tail 하고
#      Online/Offline 을 판정한다(ADR-004). 기동 전에 mosquitto 소유 644 로 미리 만든다:
#      mosquitto 는 root 로 기동해 파일을 연 뒤 mosquitto(uid 1000)로 내려가므로, HUP 때
#      다시 열려면 파일이 mosquitto 소유여야 한다. 감시 루프가 50 MB 를 넘으면 비우고
#      HUP(재오픈)한다. 백엔드 tail 은 크기 감소를 보고 처음부터 다시 읽는다.
#   D. 감시 루프 — 백엔드는 이 컨테이너에 신호를 못 보낸다(도커 소켓을 안 물린다).
#      대신 passwd.generated / aclfile.generated 를 볼륨에 떨어뜨리고, 이 루프가
#        1) 소유·권한을 mosquitto 0600 으로 맞춰 /mosquitto/data/ 로 설치
#        2) 지금 적용된 파일의 md5 를 aclfile.applied / passwd.applied 에 적는다
#        3) 브로커를 재시작한다(SIGTERM → 컨테이너 종료 → compose `restart: unless-stopped`
#           가 되살리고 이 스크립트가 설치본으로 다시 기동). eclipse-mosquitto 시절엔 HUP 였지만
#           go-auth 는 SIGHUP 에 passwd/aclfile 을 다시 읽지 않는다(플러그인의
#           mosquitto_auth_security_init(reload) 가 빈 함수, 2026-09-26 실측).
#           files 계정은 server·solarlte-test 뿐이라 바뀌는 일이 드물다(서버 비밀번호 교체,
#           공용 계정 폐기). 단말 계정은 http 백엔드라 파일과 무관하다.
#           백엔드는 passwd 를 매번 무작위 salt 로 다시 만들어 md5 가 기동마다 다르다. 그래서
#           재시작 조건은 md5 가 아니라 "aclfile 내용" 또는 "passwd 의 username 집합" 변화다.
#           해시만 바뀐 경우(서버 비밀번호 교체)는 설치만 하고 다음 재시작 때 반영된다 —
#           운영자가 `docker compose restart mosquitto` 를 한다(docs/04).
#      2) 는 백엔드가 "내보낸 것이 설치됐는지" 확인하는 용도다(mqtt_account_export).
#
# 변경 감지는 내용(md5)이다. stat mtime 은 초 단위라 같은 초에 두 번 쓰면 두 번째 내용이
# 다음 변경 때까지 설치되지 않았다(aircast 에서 겪음).
#
# 볼륨 소유: /mosquitto/dynamic 은 소유자 백엔드(uid 10001), 그룹 mosquitto(gid 1000), 2775.
# 백엔드는 소유자로 generated 를 쓰고, mosquitto 는 그룹으로 로그 파일을 만들거나 다시 연다.
# 1777(sticky) 은 쓰지 않는다 — 커널 fs.protected_regular=2 에서는 root(이 스크립트)조차
# 남의 파일을 sticky 디렉터리 안에서 열지 못해 로그 truncate 가 "Permission denied" 였다(실측).
set -eu

DYN=/mosquitto/dynamic
DATA=/mosquitto/data
SEED=/mosquitto/config
CERTS=/mosquitto/certs
EFFECTIVE="$DATA/mosquitto.effective.conf"
LOG="$DYN/mosquitto.log"
LOG_MAX_BYTES=$((50 * 1024 * 1024))
PW=/mosquitto/pw
MOSQ_USER=mosquitto          # 이 이미지에서는 uid 1000 (eclipse-mosquitto 의 1883 이 아니다)

digest() {  # 파일 내용 md5, 없으면 none
    if [ -f "$1" ]; then md5sum "$1" | cut -d' ' -f1; else echo none; fi
}

# 설치: 같은 파일시스템 안에서 임시 파일 → 원자적 교체. 리로드 순간에 반쯤 쓴
# 파일이 읽히는 일이 없게 한다.
install_file() {  # $1=원본, $2=설치 위치
    cp "$1" "$2.tmp"
    chown "$MOSQ_USER:$MOSQ_USER" "$2.tmp"
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
    chown "$MOSQ_USER:$MOSQ_USER" "$2.new"
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
# 그 사이 접속이 not authorised 로 거절된다(S2-06 에서 실제 발생). 백엔드는 둘 다 본다.
report_passwd() {  # $1=적용된 passwd md5
    echo "$1" > "$DYN/passwd.applied.tmp"
    mv "$DYN/passwd.applied.tmp" "$DYN/passwd.applied"
}

# go-auth files 형식 passwd 한 줄. pw 는 해시만 찍으므로 username 을 앞에 붙인다.
usernames_digest() {  # passwd 의 username 집합(정렬) md5 — 해시(salt)만 바뀐 것과 구분한다
    if [ -f "$1" ]; then cut -d: -f1 "$1" | sort | md5sum | cut -d' ' -f1; else echo none; fi
}

passwd_line() {  # $1=username, $2=password
    printf '%s:%s\n' "$1" "$("$PW" -p "$2")"
}

# 시드 passwd 생성 — 백엔드가 아직 한 번도 내보내지 않은 첫 기동용.
seed_passwd() {  # $1=만들 파일
    {
        # 1차 공용 시험 계정 (사양서 §1.1.2.1). 백엔드가 MQTT_TEST_ACCOUNT_ENABLED=false 로
        # generated 를 내보내면 그때 빠진다.
        passwd_line solarlte-test solarlte-test-2026
        if [ -n "${MQTT_PASSWORD:-}" ]; then
            passwd_line server "$MQTT_PASSWORD"
        else
            echo "MQTT_PASSWORD 가 없어 server 계정을 시드에 넣지 않는다 (백엔드가 generated 로 넣을 때까지 서버 접속 불가)" >&2
        fi
    } > "$1"
    echo "passwd 시드 생성: $(cut -d: -f1 "$1" | tr '\n' ' ')" >&2
}

# 부트스트랩: generated 가 있으면 그것, 없고 설치본도 없으면 시드.
#   aclfile 시드 = /mosquitto/config/aclfile (리포).
#   passwd 시드 = pw 로 즉석 생성 (리포에 비밀번호 파일을 두지 않는다).
bootstrap() {  # $1=이름(passwd|aclfile)
    if [ -f "$DYN/$1.generated" ]; then
        install_file "$DYN/$1.generated" "$DATA/$1"
    elif [ ! -f "$DATA/$1" ]; then
        if [ "$1" = passwd ]; then
            seed_passwd "$DATA/$1.tmp"
        elif [ -f "$SEED/$1" ]; then
            cp "$SEED/$1" "$DATA/$1.tmp"
        else
            : > "$DATA/$1.tmp"
        fi
        chown "$MOSQ_USER:$MOSQ_USER" "$DATA/$1.tmp"
        chmod 600 "$DATA/$1.tmp"
        mv "$DATA/$1.tmp" "$DATA/$1"
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
    chown "$MOSQ_USER:$MOSQ_USER" "$EFFECTIVE"
    chmod 644 "$EFFECTIVE"
}

# ── C. 로그 파일 준비 ────────────────────────────────────────────────
prepare_log() {
    [ -f "$LOG" ] || : > "$LOG"
    chown "$MOSQ_USER:$MOSQ_USER" "$LOG"
    chmod 644 "$LOG"     # 백엔드(uid 10001)가 읽는다
}

mkdir -p "$DYN" "$DATA"
chown "10001:$MOSQ_USER" "$DYN" || true   # 소유자 = 백엔드(uid 10001), 그룹 = mosquitto
chmod 2775 "$DYN" || true                  # 그룹(mosquitto)도 파일 생성 가능, setgid
chown -R "$MOSQ_USER:$MOSQ_USER" "$DATA" || true   # persistence(mosquitto.db) 는 mosquitto 가 쓴다

assemble_conf
prepare_log

# ── B. 부트스트랩 ────────────────────────────────────────────────────
bootstrap passwd
bootstrap aclfile

# ── D. 감시 루프 ─────────────────────────────────────────────────────
(
    last_pw="$(digest "$DATA/passwd")"
    last_pw_users="$(usernames_digest "$DATA/passwd")"
    last_acl="$(digest "$DATA/aclfile")"
    # mosquitto 가 기동하며 이 파일들을 읽는다. 기동 직후부터 적용 보고가 있어야 백엔드가
    # 기다릴 기준을 안다.
    report_acl "$last_acl"
    report_passwd "$last_pw"
    while :; do
        sleep 1
        pw_changed=0
        log_rotated=0
        pw_users_changed=0
        if cur="$(install_if_changed "$DYN/passwd.generated" "$DATA/passwd" "$last_pw")"; then
            last_pw="$cur"; pw_changed=1
            cur_users="$(usernames_digest "$DATA/passwd")"
            [ "$cur_users" = "$last_pw_users" ] || pw_users_changed=1
            last_pw_users="$cur_users"
        fi
        acl_changed=0
        if cur="$(install_if_changed "$DYN/aclfile.generated" "$DATA/aclfile" "$last_acl")"; then
            last_acl="$cur"; acl_changed=1
        fi
        # 로그 회전: 50 MB 넘으면 비우고 HUP 로 재오픈. 백업은 두지 않는다 — 접속 이력의
        # 정본은 DB(device_event) 이고, 컨테이너 stdout 로그(json-file 20 MB×5)가 따로 있다.
        log_size="$(stat -c %s "$LOG" 2>/dev/null || echo 0)"
        if [ "$log_size" -gt "$LOG_MAX_BYTES" ]; then
            : > "$LOG"
            log_rotated=1
            echo "mosquitto.log ${log_size} B > ${LOG_MAX_BYTES} B — 비우고 재오픈"
        fi
        # 보고는 설치 직후 — "내보낸 것이 디스크에 설치됐다"는 뜻이다.
        [ "$acl_changed" = 1 ] && report_acl "$last_acl"
        [ "$pw_changed" = 1 ] && report_passwd "$last_pw"
        if [ "$acl_changed" = 1 ] || [ "$pw_users_changed" = 1 ]; then
            # 되살아난 뒤 bootstrap 이 generated 를 다시 설치하고 그걸로 기동한다.
            echo "aclfile 내용 또는 passwd 계정 집합 변경 — go-auth 는 HUP 로 다시 읽지 않으므로 브로커를 재시작한다 (단말 전부 재접속)"
            kill -TERM 1
            exit 0
        elif [ "$pw_changed" = 1 ]; then
            echo "passwd 설치(해시만 변경, 계정 집합 동일) — 브로커 재시작 때 반영된다. 비밀번호를 바꿨다면: docker compose restart mosquitto"
        fi
        if [ "$log_rotated" = 1 ]; then
            kill -HUP 1          # mosquitto 가 로그 파일을 다시 연다
        fi
    done
) &

# 이 이미지에는 docker-entrypoint.sh 가 없다. mosquitto 를 PID 1 로 직접 exec 한다.
# root 로 시작해 리스너·로그 파일을 연 뒤 conf 의 `user`(기본 mosquitto)로 내려간다.
exec /usr/sbin/mosquitto -c "$EFFECTIVE"
