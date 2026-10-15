#!/usr/bin/env bash
#
# 서버 준비 — 클라우드 무관(문제점 53번). Ubuntu 22.04/24.04 LTS 새 서버에서 한 번(여러 번 실행해도 안전).
# AWS EC2·KT 클라우드·NHN·네이버 클라우드·온프레미스 어디서나 같다. 클라우드 전용 기능(메타데이터·에이전트)은 쓰지 않는다.
#
#   sudo bash setup-server.sh
#
# 하는 일: apt 기본 도구 · 시간대 KST · Docker Engine + compose 플러그인 · 사용자 docker 그룹 · (메모리 작으면) swap · ufw 포트.
# 하지 않는 일: 프로그램 설치 — 그건 납품 묶음의 scripts/install-offline.sh (docs/08).
#
# 인터넷이 막힌 서버: Docker 는 download.docker.com 에서 받는다. 막혀 있으면 기관·클라우드의 내부 저장소로
# Docker 를 먼저 설치해 두면 이 스크립트는 그 단계를 건너뛴다(이미 설치됨).
set -euo pipefail

# 메모리 4 GB 미만이면 swap 을 만든다(작은 서버에서 postgres+backend+mosquitto 가 같이 산다)
SWAP_GB="${SWAP_GB:-2}"
APP_USER="${SUDO_USER:-ubuntu}"

log() { echo -e "\n\033[1m== $*\033[0m"; }

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -E bash "$0" "$@"
fi

# ── 1. apt ───────────────────────────────────────────────────────────
log "apt 갱신"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q

# ── 시간대: KST. cron(백업 03:10·점검 08:30·비용 09:00·인증서 갱신 06:30)과 로그를 한국 시각으로 본다.
#    (앱은 시각을 UTC 로 저장하고 KST 로 계산하므로 서버 시간대와 무관하다 — 2026-10-03)
if [ "$(timedatectl show -p Timezone --value 2>/dev/null)" != "Asia/Seoul" ]; then
    timedatectl set-timezone Asia/Seoul
    systemctl restart cron 2>/dev/null || true
    echo "시간대 → Asia/Seoul"
fi
apt-get install -y -q ca-certificates curl gnupg git ufw jq

# ── 2. Docker ────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    log "Docker 설치"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
else
    log "Docker 이미 설치됨: $(docker --version)"
fi
docker compose version

# ── 3. docker 그룹 ───────────────────────────────────────────────────
if id -nG "$APP_USER" | grep -qw docker; then
    echo "   $APP_USER 는 이미 docker 그룹"
else
    log "$APP_USER 를 docker 그룹에 추가 (재로그인 필요)"
    usermod -aG docker "$APP_USER"
fi

# ── 4. swap ──────────────────────────────────────────────────────────
MEM_GB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
if [ "$MEM_GB" -ge 4 ]; then
    log "메모리 ${MEM_GB} GB — swap 생략"
elif swapon --show | grep -q '^/swapfile'; then
    log "swap 이미 활성"
else
    log "swap ${SWAP_GB} GB 생성"
    fallocate -l "${SWAP_GB}G" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # 메모리가 부족할 때만 swap 을 쓰게. 기본 60 은 너무 이르다.
    sysctl -w vm.swappiness=10
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi

# ── 5. ufw ───────────────────────────────────────────────────────────
# OS 방화벽은 포트 단위만 연다. 접속원 IP 제한은 클라우드 방화벽(보안 그룹·ACL — AWS SG, KT 클라우드 방화벽 등)에서 한다:
#   1883 (평문, 1차~3차) — 시험 단말 회선(통신사 NAT 대역)과 시험 PC IP 로만 제한할 것.
#                         4차 이후 운영 계정을 거부하고 호스트 공개를 닫는다.
#   8883 (TLS, 4차)      — 인터넷 전체 허용 가능.
#   80/443               — certbot HTTP-01 과 3차 이후 관리 화면.
#   22                   — 관리자 IP 만.
log "ufw"
ufw allow 22/tcp    >/dev/null
ufw allow 1883/tcp  >/dev/null
ufw allow 8883/tcp  >/dev/null
ufw allow 80/tcp    >/dev/null
ufw allow 443/tcp   >/dev/null
ufw --force enable  >/dev/null
ufw status numbered


log "서버 준비 완료"
cat <<EOF
다음:
  1. (docker 그룹 반영) 로그아웃 후 다시 접속
  2. 납품 묶음을 올리고 풀기:  tar -xzf iotlight-<버전>.tar.gz -C /opt && mv /opt/iotlight-<버전> /opt/solar_power_control
  3. cd /opt/solar_power_control && bash scripts/install-offline.sh   (처음엔 .env 를 만들고 멈춘다 → 채우고 다시)
  4. 클라우드 방화벽: 8883(단말)·443/80(관리 화면) 열기, 22 는 관리자 IP 만, 1883 은 닫기(시험 때만)
EOF
