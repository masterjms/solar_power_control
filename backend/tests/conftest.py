"""순수 단위 시험 공통 설정. DB·브로커 없이 돈다.

app.config 가 리포지토리 루트의 .env 를 읽으므로, 개발자 .env 의 값이 시험 결과를 바꾸지
않도록 환경을 고정한다(import 전에 설정해야 한다).
"""

from __future__ import annotations

import os

os.environ.setdefault("MQTT_TOPIC_ROOT", "iotlight")
os.environ.setdefault("MQTT_USERNAME", "server")
os.environ.setdefault("MQTT_PASSWORD", "server-pw")
os.environ.setdefault("MQTT_TEST_ACCOUNT_ENABLED", "false")
os.environ.setdefault("DEVICE_ONLINE_FACTOR", "3")
os.environ.setdefault("MOSQUITTO_PASSWD_EXPORT", "")
os.environ.setdefault("MOSQUITTO_ACL_EXPORT", "")
