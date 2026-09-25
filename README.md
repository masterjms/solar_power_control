# solar_power_control — 태양광 조명 관제 서버

태양광 골목길 가로등·난간 LED 조명(STM32 + WD-N522S LTE Cat.M1 모뎀)을 MQTT 로 관제하는 서버.

- 단말 프로토콜 원본: [docs/spec/태양광_조명_서버_관제_사양_v1.0.md](docs/spec/태양광_조명_서버_관제_사양_v1.0.md) (단말측 관리, 서버가 임의로 바꾸지 않는다)
- 1차 접속시험 절차: [docs/spec/1차_MQTT_접속시험_README.md](docs/spec/1차_MQTT_접속시험_README.md)
- 서버 개발 계획·영역 구분: [docs/00_개발계획.md](docs/00_개발계획.md)

## 구성

```
backend/        FastAPI + aiomqtt 수집·제어 서비스 (2차~)
infra/          Mosquitto 설정, 서버 셋업 스크립트, AWS 구성
tools/sim/      단말 시뮬레이터 (LTE 모뎀 없이 사양대로 동작하는 가짜 단말)
tools/scenarios/ 사용자 시나리오 자동 시험 (큰 단계가 끝날 때마다 돌린다)
scripts/        운영·점검 스크립트
docs/           설계 문서 (영역별 1장씩), spec/ 은 받은 원본
```

## 빠른 시작 (로컬)

```bash
cp .env.example .env
docker compose --profile local-db up -d          # postgres + mosquitto + backend
python tools/scenarios/run.py --all              # 시나리오 전체 실행
```

자세한 것은 [docs/04_인프라_운영.md](docs/04_인프라_운영.md).
