"""단말 송신 직후 CONFIG_SET 을 보낼지 정하고 cv_server 를 확정한다 (사양서 §1.1.7 S-13, §1.1.10).

REGISTER 핸들러와 Telemetry flush 가 같은 판정을 써야 하므로 한 곳에 둔다. 판정 자체는
core/config_rules 의 순수 함수이고, 여기서는 그것을 DB 행에 적용해 (1) cv_server 를 올려
쓰고 (2) ConfigSyncQueue 에 잡을 넣는다.

    state != ACTIVE            → 아무것도 안 한다 (단말이 STATE 로 거부, S-10)
    cv_device 없음             → 안 한다 (1차 펌웨어)
    cv_device == cv_server     → 안 한다 (동기화됨)
    그 외                      → cv_server = next_cv_server(cv_server, cv_device) 로 확정·저장
                                 → 큐 (60초 쿨다운은 큐가 본다)

cv_server 확정을 큐에 넣기 **전**에 DB 에 쓰는 이유: 발행이 늦거나 실패해도 서버 의도값은
이미 1 이상·단말보다 크게 굳어 있어야 다음 Telemetry 의 비교가 같은 답을 낸다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config_rules import effective_config, next_cv_server, should_send_config
from app.models.device import Device
from app.models.profile import ConfigProfile
from app.mqtt.config_sync import ConfigJob, ConfigSyncQueue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceConfigRow:
    """판정에 필요한 device 컬럼만. RETURNING / SELECT 결과에서 만든다."""

    uuid: str
    state: str
    cv_server: int
    cv_device: int | None
    ti_override: int | None
    ka_override: int | None
    profile_id: int
    lat: float | None
    lon: float | None


#: SELECT/RETURNING 에 넣을 컬럼 순서. DeviceConfigRow(*row) 로 바로 만든다.
CONFIG_COLUMNS = (
    Device.uuid, Device.state, Device.cv_server, Device.cv_device,
    Device.ti_override, Device.ka_override, Device.profile_id, Device.lat, Device.lon,
)


async def load_profiles(db: AsyncSession) -> dict[int, tuple[int, int]]:
    """{profile_id: (ti, ka)}. 프로필은 몇 개 안 되므로 판정마다 통째로 읽는다."""
    rows = await db.execute(select(ConfigProfile.id, ConfigProfile.ti, ConfigProfile.ka))
    return {pid: (ti, ka) for pid, ti, ka in rows}


async def decide_and_enqueue(
    db: AsyncSession,
    config_sync: ConfigSyncQueue | None,
    rows: list[DeviceConfigRow],
    *,
    reason: str,
    profiles: dict[int, tuple[int, int]] | None = None,
) -> list[ConfigJob]:
    """행마다 판정 → cv_server 갱신(필요 시) → 큐. 만든 잡 목록을 돌려준다(테스트·로그용).

    profiles 를 안 주면 여기서 읽는다. 같은 트랜잭션 안에서 불러야 cv_server 갱신이 함께 커밋된다.
    """
    jobs: list[ConfigJob] = []
    if not rows:
        return jobs
    if profiles is None:
        profiles = await load_profiles(db)
    for row in rows:
        if not should_send_config(row.state, row.cv_server, row.cv_device):
            continue
        profile = profiles.get(row.profile_id)
        if profile is None:
            # FK 가 막지만, 프로필 캐시가 옛것일 수 있다(방금 만든 프로필). 다음 송신 때 다시.
            log.warning("프로필 %s 없음 — %s CONFIG 판정 건너뜀", row.profile_id, row.uuid)
            continue
        cv = next_cv_server(row.cv_server, row.cv_device)
        if cv != row.cv_server:
            await db.execute(
                update(Device).where(Device.uuid == row.uuid).values(cv_server=cv)
            )
        eff = effective_config(
            ti_override=row.ti_override, ka_override=row.ka_override,
            profile_ti=profile[0], profile_ka=profile[1],
        )
        job = ConfigJob(row.uuid, cv, eff.ti, eff.ka, row.lat, row.lon, reason)
        jobs.append(job)
        if config_sync is not None:
            config_sync.offer(job)
    return jobs
