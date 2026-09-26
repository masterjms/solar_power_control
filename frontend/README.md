# frontend — 관리 화면

REST API(`docs/05_API.md`)를 목업(`docs/spec/ui/solar_light_dashboard_v15.html`)과 같은 틀로 보여주고 승인·설정·프로필을 조작하는 화면.
화면 지도와 자리표시 목록은 `docs/07_프론트엔드.md`.
Vite + React + TypeScript, UI·차트 라이브러리 없음, `fetch` 와 CSS 만.

## 실행

```
cd frontend
npm install
npm run dev                          # http://localhost:5173 (다른 포트: npm run dev -- --port 5175 --strictPort)
```

백엔드가 `http://localhost:8000` 에 떠 있어야 한다(`docker compose -f docker-compose.dev.yml up` 또는 uvicorn).

## 프록시

`vite.config.ts` 의 dev 프록시가 `/api`, `/health` 를 `http://localhost:8000` 으로 넘긴다.
그래서 코드에서는 상대 경로(`/api/devices`)만 쓰고 CORS 설정이 필요 없다.
운영은 nginx 가 `dist/` 를 서빙하고 `/api` 를 백엔드로 프록시한다.

## 빌드

```
npm run build    # tsc 타입검사 + dist/ 생성
```

## 파일

- `index.html` — 테마 초기화(`localStorage slc-theme`, 목업과 같음) + Noto Sans KR
- `src/style.css` — 목업 v15 의 CSS 변수·클래스 그대로 (+ `.ph` 자리표시)
- `src/ui.tsx` — Card / Placeholder / StateBadge / OnlineMark / Battery / Lamp / Met
- `src/api.ts` — REST 호출과 응답 타입, 에러(`{error:{code,message}}`) 파싱
- `src/format.ts` — x100 단위 변환, `er` 비트 해석, 상대시간, 이벤트 요약
- `src/App.tsx` — 사이드바 + 헤더 + 해시 라우팅(#dash/#devices/#pending/#config/#profiles/#system) + 드로어
- `src/Dashboard.tsx` — 대시보드(단말 상태·조명·배터리·조치 필요는 실제, 지도·발전·이벤트는 자리표시)
- `src/DeviceList.tsx` — 단말 목록(서버 필터·페이지, ti/ka 의도·보고) → 행 클릭으로 드로어
- `src/DeviceDetail.tsx` — 드로어: 승인 패널·설정 패널·PING/삭제·단말기 정보·텔레메트리·이벤트
- `src/Pending.tsx` — 단말 등록·승인(PENDING 표 + 행 안 승인/거부, 보낼 REGISTER_ACK/CONFIG_SET 미리보기)
- `src/DeviceConfig.tsx` — 단말 설정(5·6차 자리표시)
- `src/Profiles.tsx` — 프로필(표 안 편집·추가·삭제)
- `src/System.tsx` — 시스템(/health 타일, /health·/api/metrics JSON)
