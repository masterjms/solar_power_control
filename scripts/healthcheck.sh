#!/usr/bin/env bash
#
# 서버 내부 점검 (배포 직후 확인 + cron 일일 점검)
#
#   bash scripts/healthcheck.sh            화면 출력, 이상이 있으면 종료코드 1
#   bash scripts/healthcheck.sh --slack    이상이 있을 때만 Slack (.env 의 SLACK_WEBHOOK_URL)
#   bash scripts/healthcheck.sh --slack --always   정상이어도 보낸다 (일일 요약)
#
# 보는 것:
#   · 디스크·메모리        gp3 30 GB 에 telemetry 파티션 + 컨테이너 로그가 쌓인다
#   · 컨테이너 상태        호스트는 멀쩡한데 컨테이너만 죽은 경우
#   · mosquitto 1883/8883  리스너가 실제로 듣고 있는지 (8883 은 인증서가 있을 때만)
#   · backend /health      DB·MQTT 까지 정상인지 (2차부터. 없으면 경고만)
#   · passwd/aclfile 적용  aclfile.applied 가 generated 와 같은지 (2차)
#   · 백업 최신성          pg_dump cron 이 멈춘 걸 알아채기 위해
#   · 인증서 만료          4차. 없으면 정보만
#
# "서버가 죽었는가" 는 여기서 못 본다 — 그건 CloudWatch StatusCheckFailed 담당.
set -uo pipefail          # -e 는 쓰지 않는다. 한 항목이 실패해도 나머지는 점검한다.

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SLACK=0; ALWAYS=0
for arg in "$@"; do
    case "$arg" in
        --slack)  SLACK=1 ;;
        --always) ALWAYS=1 ;;
        --dry)    ;;                     # 예전 호환 — 기본이 화면 출력이다
        *) echo "알 수 없는 옵션: $arg"; exit 2 ;;
    esac
done

DISK_WARN="${DISK_WARN:-80}"          # %
MEM_WARN="${MEM_WARN:-90}"            # % (t3.small 2 GB + swap. 85 는 평소에도 넘는다)
BACKUP_MAX_AGE_H="${BACKUP_MAX_AGE_H:-30}"
CERT_WARN_DAYS="${CERT_WARN_DAYS:-20}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/db-backups}"

PROBLEMS=(); LINES=()
add_ok()   { LINES+=("  [OK]   $1"); }
add_bad()  { LINES+=("  [FAIL] $1"); PROBLEMS+=("$1"); }
add_warn() { LINES+=("  [WARN] $1"); PROBLEMS+=("$1"); }
add_info() { LINES+=("  [--]   $1"); }

# ── 디스크 ──────────────────────────────────────────────────────────
DISK_PCT="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
DISK_FREE="$(df -h --output=avail / | tail -1 | tr -d ' ')"
if [ "${DISK_PCT:-0}" -ge "$DISK_WARN" ]; then
    add_bad "디스크 ${DISK_PCT}% 사용 (여유 ${DISK_FREE})"
else
    add_ok "디스크 ${DISK_PCT}% (여유 ${DISK_FREE})"
fi
# docker 가 차지하는 양 — 로그 로테이션이 빠졌거나 이미지가 쌓이면 여기서 보인다
DOCKER_DU="$(docker system df --format '{{.Type}} {{.Size}}' 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
[ -n "$DOCKER_DU" ] && add_info "docker: $DOCKER_DU"

# ── 메모리 ──────────────────────────────────────────────────────────
MEM_PCT="$(free 2>/dev/null | awk '/^Mem:/ {printf "%d", $3/$2*100}')"
SWAP_USED="$(free -m 2>/dev/null | awk '/^Swap:/ {print $3}')"
if [ -z "$MEM_PCT" ]; then
    add_warn "메모리 사용률을 읽지 못했다"
elif [ "$MEM_PCT" -ge "$MEM_WARN" ]; then
    add_warn "메모리 ${MEM_PCT}% (swap ${SWAP_USED:-?} MB)"
else
    add_ok "메모리 ${MEM_PCT}% (swap ${SWAP_USED:-?} MB)"
fi

# ── 컨테이너 ────────────────────────────────────────────────────────
# postgres 는 local-db 프로파일. RDS 전환 후엔 목록에서 뺀다.
EXPECTED=(iotlight-mosquitto iotlight-backend iotlight-postgres)
DOWN=()
for name in "${EXPECTED[@]}"; do
    state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$state" ] || state="없음"
    [ "$state" = "running" ] || DOWN+=("$name($state)")
done
if [ ${#DOWN[@]} -gt 0 ]; then
    # 1차엔 backend 가 없는 게 정상 — backend 만 없으면 경고로 낮춘다
    if [ ${#DOWN[@]} -eq 1 ] && [[ "${DOWN[0]}" == iotlight-backend* ]]; then
        add_warn "backend 컨테이너 ${DOWN[0]} (1차엔 정상)"
    else
        add_bad "컨테이너 이상: ${DOWN[*]}"
    fi
else
    add_ok "컨테이너 ${#EXPECTED[@]}개 정상"
fi

# ── mosquitto 리스너 ────────────────────────────────────────────────
listening() { ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }
if listening 1883; then add_ok "mosquitto 1883 listening"; else add_bad "1883 을 듣는 프로세스가 없다"; fi
if [ -f infra/certs/server.crt ]; then
    if listening 8883; then add_ok "mosquitto 8883 listening (TLS)"; else add_bad "인증서는 있는데 8883 을 안 듣는다 — mosquitto 재시작 필요(리스너는 HUP 로 안 생긴다)"; fi
else
    add_info "8883 비활성 (infra/certs/server.crt 없음 — 4차 전 정상)"
fi
# fd 사용량 — 1만 대 목표. soft 65536 이 실제 적용됐는지
MOSQ_PID="$(docker inspect -f '{{.State.Pid}}' iotlight-mosquitto 2>/dev/null || true)"
if [ -n "$MOSQ_PID" ] && [ "$MOSQ_PID" != "0" ] && [ -r "/proc/$MOSQ_PID/limits" ]; then
    NOFILE="$(awk '/Max open files/ {print $4}' "/proc/$MOSQ_PID/limits")"
    NFD="$(sudo -n ls "/proc/$MOSQ_PID/fd" 2>/dev/null | wc -l || echo '?')"
    if [ "${NOFILE:-0}" -ge 65536 ] 2>/dev/null; then
        add_ok "mosquitto fd 한도 ${NOFILE}, 사용 ${NFD}"
    else
        add_warn "mosquitto fd 한도 ${NOFILE:-?} (65536 이어야 함 — compose ulimits 확인)"
    fi
fi

# ── backend /health ─────────────────────────────────────────────────
HEALTH="$(docker compose exec -T backend python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health',timeout=5).read().decode())" 2>/dev/null || echo '')"
case "$HEALTH" in
    *'"status":"ok"'*|*'"status": "ok"'*) add_ok "backend /health ok" ;;
    *'"status"'*)                          add_bad "backend /health: $HEALTH" ;;
    *)                                     add_warn "backend /health 응답 없음 (1차엔 정상)" ;;
esac

# ── passwd/aclfile 적용 상태 (2차) ──────────────────────────────────
GEN_MD5="$(docker compose exec -T mosquitto sh -c 'f=/mosquitto/dynamic/aclfile.generated; [ -f $f ] && md5sum $f | cut -d" " -f1' 2>/dev/null | tr -d '[:space:]')"
APP_MD5="$(docker compose exec -T mosquitto sh -c 'cat /mosquitto/dynamic/aclfile.applied 2>/dev/null' 2>/dev/null | tr -d '[:space:]')"
if [ -n "$GEN_MD5" ]; then
    if [ "$GEN_MD5" = "$APP_MD5" ]; then add_ok "aclfile.generated 적용됨 (${APP_MD5:0:8})"
    else add_bad "aclfile.generated(${GEN_MD5:0:8}) ≠ applied(${APP_MD5:0:8}) — entrypoint 감시 루프 확인"; fi
else
    add_info "aclfile.generated 없음 (백엔드 계정 내보내기 전 — 시드 ACL 사용 중)"
fi

# ── 백업 최신성 ─────────────────────────────────────────────────────
LATEST="$(ls -t "$BACKUP_DIR"/iotlight-*.sql.gz 2>/dev/null | head -1 || true)"
if [ -z "$LATEST" ]; then
    add_warn "DB 백업이 하나도 없다 ($BACKUP_DIR) — cron 설정 확인(docs/04 §백업)"
else
    AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$LATEST") ) / 3600 ))
    SIZE="$(du -h "$LATEST" | cut -f1)"
    if [ "$AGE_H" -gt "$BACKUP_MAX_AGE_H" ]; then
        add_bad "최근 백업이 ${AGE_H}시간 전 — cron 이 멈췄을 수 있다"
    else
        add_ok "백업 ${AGE_H}시간 전 ($SIZE)"
    fi
fi

# ── 인증서 만료 ─────────────────────────────────────────────────────
CERT=infra/certs/server.crt
if [ -f "$CERT" ]; then
    END="$(openssl x509 -enddate -noout -in "$CERT" 2>/dev/null | cut -d= -f2)"
    if [ -n "$END" ]; then
        DAYS=$(( ( $(date -d "$END" +%s) - $(date +%s) ) / 86400 ))
        if [ "$DAYS" -le "$CERT_WARN_DAYS" ]; then add_bad "TLS 인증서 만료까지 ${DAYS}일 — 자동 갱신 확인"
        else add_ok "TLS 인증서 ${DAYS}일 남음"; fi
    fi
fi

# ── 출력 ────────────────────────────────────────────────────────────
if [ ${#PROBLEMS[@]} -gt 0 ]; then HEAD="서버 점검 — 확인 필요 ${#PROBLEMS[@]}건 ($(hostname))"
else HEAD="서버 점검 — 모두 정상 ($(hostname))"; fi
TEXT="$HEAD"$'\n'"$(printf '%s\n' "${LINES[@]}")"
echo "$TEXT"

if [ "$SLACK" -eq 1 ] && { [ ${#PROBLEMS[@]} -gt 0 ] || [ "$ALWAYS" -eq 1 ]; }; then
    WEBHOOK="$(grep -E '^SLACK_WEBHOOK_URL=' .env 2>/dev/null | cut -d= -f2- | tr -d "\"'" || true)"
    if [ -n "$WEBHOOK" ]; then
        curl -sS -X POST -H 'Content-type: application/json' \
             --data "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.stdin.read()}))' <<< "$TEXT")" \
             "$WEBHOOK" >/dev/null && echo "(Slack 전송)"
    else
        echo "(.env 에 SLACK_WEBHOOK_URL 이 없어 Slack 생략)"
    fi
fi

[ ${#PROBLEMS[@]} -eq 0 ]
