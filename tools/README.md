# tools — 단말 시뮬레이터 · 시나리오 시험

backend/infra 와 별도 venv(`tools/.venv`, Python 3.10+). 브로커·DB·REST 로만 말하고 백엔드 코드를 import 하지 않는다.

```bash
python -m venv tools/.venv && tools/.venv/Scripts/python -m pip install -r tools/requirements.txt

tools/.venv/Scripts/python -m pytest tools/tests                       # 시뮬레이터 단위 시험(서비스 불필요)
tools/.venv/Scripts/python -m tools.scenarios.run --list               # 시나리오 목록
tools/.venv/Scripts/python -m tools.scenarios.run --all --report tools/scenarios/out/report.md
tools/.venv/Scripts/python -m tools.sim.fleet --count 100 --ti 60              # 가짜 단말 100대(1.4.0: HMAC 계정, 승인은 서버가)
tools/.venv/Scripts/python -m tools.sim.fleet gen-passwords --count 3          # uuid,HMAC 비밀번호
tools/.venv/Scripts/python -m tools.sim.monitor                        # iotlight/# 감시 + 사양 검증
```

| 경로 | 내용 |
|---|---|
| `sim/device.py` | 단말 한 대 모델 `SimDevice`(펌웨어 1.4.0) — HMAC 계정, REGISTER(ka)/TELEMETRY/PONG/CONFIG_ACK(OK·RANGE·STATE·FLASH)/CMD_ACK, 승인 게이트, 재접속 표, 고장 주입 |
| `sim/fleet.py` | N 대 일괄 실행 CLI(`--ka`, `--hmac-key`, `--time-scale`), `gen-passwords`, 폭주 재접속(`--cut-after`) |
| `sim/monitor.py` | 브로커 감시·사양 위반 검출 |
| `sim/env.py` | 환경 변수(`MQTT_HOST`, `MQTT_HMAC_KEY`, `DATABASE_URL`, `BACKEND_URL` …) |
| `scenarios/run.py` | 러너(`--all/--only/--list/--phase-only/--phase 5/--allow-docker/--report`) |
| `scenarios/phase2.py` `phase3.py` `phase5.py` | 시나리오 본체 |
| `scenarios/framework.py` `services.py` `common.py` | 등록·판정·보고서 / DB·REST·MQTT·docker 핸들 / 단말 생성·metrics 도우미 |
| `tests/` | pytest |

자세한 절차·합격 기준·환경 변수·백엔드 가정은 [docs/06_시험_시나리오.md](../docs/06_시험_시나리오.md).
