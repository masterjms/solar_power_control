import { Card, PlaceholderCard } from "./ui";

/** 목업 "단말 설정" 화면 — 밝기/다단계/배터리 보호/1년 스케줄은 5·6차(CMD·SCHEDULE). 지금은 자리만.
    ti/ka/프로필/좌표 같은 서버 설정은 단말 목록 → 드로어의 설정 패널에서 한다. */
export default function DeviceConfig() {
  return (
    <div className="content">
      <Card title="단말 설정" className="full" meta="단말에서 읽기 · 단말에 쓰기 · Flash 저장 · 템플릿">
        <div className="cap">
          이 화면은 단말 펌웨어 설정(밝기·다단계·배터리 보호·1년 스케줄)을 STATUS_GET / CMD / SCHEDULE 로 읽고 쓰는 곳이다 — 5·6차.
          지금 서버가 가진 설정(프로필 ti/ka, override, 좌표, 시설명)은 <a href="#devices">단말 목록</a> 에서 행을 눌러 드로어의 설정 패널로 바꾼다.
        </div>
      </Card>
      <div className="row3 full">
        <PlaceholderCard title="밝기" meta="채널 기준" className="h200" stage="6차" note="원격 밝기(COMMAND act=pwm)는 그룹 제어 · 단말 드로어에서 보낸다. 설치 기준 밝기 설정은 6차" />
        <PlaceholderCard title="다단계 밝기" meta="밤사이 변경 시각" className="h200" stage="6차" />
        <PlaceholderCard title="배터리 보호" meta="V / 분" className="h200" stage="6차" />
      </div>
      <PlaceholderCard title="1년 스케줄" meta="일출·일몰 표" className="h200" stage="6차" note="SCHEDULE chunk" />
      <PlaceholderCard title="통신 기록" meta="config / result" className="h200" stage="6차" note="단말별 이벤트는 드로어에서 볼 수 있다" />
    </div>
  );
}
