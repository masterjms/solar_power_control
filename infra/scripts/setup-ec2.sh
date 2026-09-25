#!/usr/bin/env bash
#
# 새 EC2(Ubuntu 24.04, t3.small) 최초 셋업. 여러 번 실행해도 안전하다(멱등).
#
#   ssh ubuntu@<EIP>
#   curl -fsSL https://raw.githubusercontent.com/masterjms/solar_power_control/main/infra/scripts/setup-ec2.sh | bash
#   또는 저장소를 먼저 받아서:  bash infra/scripts/setup-ec2.sh
#
# 하는 일:
#   1. apt 갱신 + 기본 도구
#   2. Docker Engine + compose 플러그인 (docker 공식 apt 저장소)
#   3. ubuntu 를 docker 그룹에 (재로그인 후 sudo 없이 docker)
#   4. swap 2 GB (t3.small 메모리 2 GB — postgres+backend+mosquitto 가 같이 산다)
#   5. ufw: 22 / 1883 / 8883 / 80 / 443
#   6. 저장소 clone → /opt/solar_power_control, .env 가 없으면 견본 복사
#   7. docker compose --profile local-db up -d
#
# 끝나면 .env 의 비밀번호를 채우고 docs/04_인프라_운영.md "1차 런북"으로 넘어간다.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/masterjms/solar_power_control.git}"
APP_DIR="${APP_DIR:-/opt/solar_power_control}"
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
if swapon --show | grep -q '^/swapfile'; then
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
# OS 방화벽은 포트 단위만 연다. 접속원 IP 제한은 AWS Security Group 에서 한다:
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

# ── 6. 저장소 ────────────────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    log "저장소 있음: $APP_DIR (갱신은 scripts/deploy.sh 로)"
else
    log "clone → $APP_DIR"
    git clone "$REPO_URL" "$APP_DIR"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
fi
cd "$APP_DIR"

if [ ! -f .env ]; then
    log ".env 생성 (견본 복사)"
    cp .env.example .env
    chown "$APP_USER:$APP_USER" .env
    chmod 600 .env
    echo "   !! .env 의 change-me-* 비밀번호를 채워야 한다: nano $APP_DIR/.env"
else
    echo "   .env 있음 — 그대로 둔다"
fi

# 인증서 디렉터리는 비어 있어도 마운트되어야 한다(4차 전엔 비어 있는 게 정상).
mkdir -p infra/certs
chown "$APP_USER:$APP_USER" infra/certs

# ── 7. 기동 ──────────────────────────────────────────────────────────
log "docker compose up (mosquitto + postgres; backend 는 이미지가 있을 때만)"
# 1차 시점엔 backend/ 가 아직 빌드되지 않을 수 있다. mosquitto 와 postgres 먼저 올린다.
docker compose --profile local-db up -d mosquitto postgres
if [ -f backend/Dockerfile ]; then
    docker compose --profile local-db up -d --build backend || \
        echo "   backend 기동 실패 — 1차에는 필요 없다. 2차에서 scripts/deploy.sh 로 다시."
fi
docker compose ps

cat <<EOF

셋업 완료.
다음:
  1. (docker 그룹 반영) 로그아웃 후 다시 ssh
  2. nano $APP_DIR/.env   — change-me-* 채우기
  3. bash infra/scripts/mosquitto-passwd-add.sh solarlte-test solarlte-test-2026
     bash infra/scripts/mosquitto-passwd-add.sh server '<MQTT_PASSWORD>'
  4. AWS Security Group 에서 1883 을 시험 접속원 IP 로만 열기
  5. docs/04_인프라_운영.md "1차 런북" 진행
EOF
