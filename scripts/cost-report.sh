#!/usr/bin/env bash
#
# AWS 비용 일일 요약 → Slack (cron 으로 매일 아침 실행)
#
#   bash scripts/cost-report.sh          Slack 으로 보낸다
#   bash scripts/cost-report.sh --dry    화면에만 출력 (설정 확인용)
#
# 필요한 것:
#   1. .env 에 SLACK_WEBHOOK_URL
#   2. EC2 IAM 역할에 ce:GetCostAndUsage 권한
#      (EC2 에 인스턴스 역할이 없으면 하나 만들어 붙인다 — docs/04 §11.2)
#
# 왜 aws CLI 를 설치하지 않고 컨테이너로 부르나:
#   서버에 패키지를 하나라도 덜 얹기 위해서다. 자격증명은 인스턴스 역할을
#   컨테이너가 그대로 쓰므로 키 파일이 필요 없다.
#
# Cost Explorer API 는 호출당 $0.01 이다. 하루 한 번이면 월 $0.3 수준.
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

DRY=0
[ "${1:-}" = "--dry" ] && DRY=1

[ -f .env ] || { echo "!! .env 가 없다"; exit 1; }
WEBHOOK="$(grep -E '^SLACK_WEBHOOK_URL=' .env | cut -d= -f2- | tr -d '"'"'"'' || true)"
if [ "$DRY" -eq 0 ] && [ -z "$WEBHOOK" ]; then
    echo "!! .env 에 SLACK_WEBHOOK_URL 이 없다"
    exit 1
fi

# Cost Explorer 의 End 는 제외(exclusive)다. 어제 하루치를 보려면 Start=어제, End=오늘.
YESTERDAY="$(date -d yesterday +%F)"
TODAY="$(date +%F)"
MONTH_START="$(date +%Y-%m-01)"

aws_ce() {
    docker run --rm amazon/aws-cli:latest ce "$@" --output json 2>/dev/null
}

# 어제치 — 서비스별로 나눠서 어디에 나갔는지 본다.
DAILY_JSON="$(aws_ce get-cost-and-usage \
    --time-period "Start=$YESTERDAY,End=$TODAY" \
    --granularity DAILY --metrics UnblendedCost \
    --group-by Type=DIMENSION,Key=SERVICE)" || {
        echo "!! Cost Explorer 호출 실패 — IAM 권한(ce:GetCostAndUsage)을 확인할 것"
        exit 1
    }

# 이번 달 누적. 오늘은 데이터가 아직 안 찼으므로 제외한다.
MONTH_JSON="$(aws_ce get-cost-and-usage \
    --time-period "Start=$MONTH_START,End=$TODAY" \
    --granularity MONTHLY --metrics UnblendedCost)"

TEXT="$(DAILY="$DAILY_JSON" MONTHLY="$MONTH_JSON" python3 - <<'PY'
import json, os, datetime, calendar

daily = json.loads(os.environ["DAILY"])
monthly = json.loads(os.environ["MONTHLY"])

# ── 어제: 서비스별 ────────────────────────────────────────────────
groups = []
day_total = 0.0
for result in daily.get("ResultsByTime", []):
    for g in result.get("Groups", []):
        amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
        if amount < 0.005:          # 반올림하면 0.00 인 항목은 줄만 늘린다
            continue
        name = g["Keys"][0]
        # 서비스 이름이 길어서 화면을 잡아먹는다. 알아볼 만큼만 줄인다.
        for long, short in (
            ("Amazon Elastic Compute Cloud - Compute", "EC2"),
            ("EC2 - Other", "EBS·IP"),
            ("Amazon Simple Storage Service", "S3"),
            ("Amazon Relational Database Service", "RDS"),
            ("AWS Cost Explorer", "CostExplorer"),
            ("Amazon Virtual Private Cloud", "VPC"),
        ):
            if name.startswith(long):
                name = short
                break
        groups.append((name, amount))
        day_total += amount
groups.sort(key=lambda x: -x[1])

# ── 이번 달 누적 + 이 추세면 얼마가 될지 ──────────────────────────
month_total = 0.0
for result in monthly.get("ResultsByTime", []):
    month_total += float(result["Total"]["UnblendedCost"]["Amount"])

today = datetime.date.today()
days_done = today.day - 1              # 오늘은 데이터가 안 찼으니 뺀다
days_in_month = calendar.monthrange(today.year, today.month)[1]
projected = (month_total / days_done * days_in_month) if days_done > 0 else 0.0

lines = [
    f"*AWS 비용* · {today:%m월 %d일}",
    f"어제 `${day_total:,.2f}`  |  이번 달 누적 `${month_total:,.2f}`",
]
if projected:
    lines.append(f"이 추세면 이번 달 `${projected:,.0f}` 예상")
if groups:
    detail = " · ".join(f"{n} ${a:,.2f}" for n, a in groups[:6])
    lines.append(f"```{detail}```")

print("\n".join(lines))
PY
)"

if [ "$DRY" -eq 1 ]; then
    echo "$TEXT"
    exit 0
fi

curl -sS -X POST -H 'Content-type: application/json' \
     --data "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.stdin.read()}))' <<< "$TEXT")" \
     "$WEBHOOK" >/dev/null

echo "전송 완료"
