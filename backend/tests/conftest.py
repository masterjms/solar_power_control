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
os.environ.setdefault("PENDING_OFFLINE_SEC", "4200")
os.environ.setdefault("MOSQUITTO_PASSWD_EXPORT", "")
os.environ.setdefault("MOSQUITTO_ACL_EXPORT", "")
os.environ.setdefault("MOSQUITTO_LOG_PATH", "")
# 사양서 §1.1.2.2 공개 시험 키. 운영 .env 의 K1 이 시험에 섞이지 않게 고정한다.
os.environ["MQTT_HMAC_KEYS"] = (
    "TEST:000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
)
os.environ["MQTT_AUTH_SHARED_SECRET"] = ""
