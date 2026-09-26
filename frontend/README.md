# frontend — 개발용 최소 화면

REST API(`docs/05_API.md`)를 눈으로 확인하고 승인·프로필을 조작하기 위한 개발용 화면(사양서 §3.9.2). 예쁠 필요 없음, 기능만.
Vite + React + TypeScript, UI 라이브러리 없음, `fetch` 만 사용.

## 실행

```
cd frontend
npm install
npm run dev      # http://localhost:5173
```

백엔드가 `http://localhost:8000` 에 떠 있어야 한다(`docker compose -f docker-compose.dev.yml up` 또는 uvicorn).

## 프록시

`vite.config.ts` 의 dev 프록시가 `/api`, `/health` 를 `http://localhost:8000` 으로 넘긴다.
그래서 코드에서는 상대 경로(`/api/devices`)만 쓰고 CORS 설정이 필요 없다.
운영 배포(nginx 가 `dist/` 서빙 + `/api` 프록시)는 3차에서.

## 빌드

```
npm run build    # tsc 타입검사 + dist/ 생성
```

## 파일

- `src/api.ts` — REST 호출과 응답 타입, 에러(`{error:{code,message}}`) 파싱
- `src/format.ts` — x100 단위 변환, `er` 비트 해석, 상대시간
- `src/App.tsx` — 상단 요약바(목록 counts + /health), 탭(#devices / #profiles / #system), 좌우 분할
- `src/DeviceList.tsx` — 왼쪽 단말 목록(state/online/q 서버 필터·페이지, ti/ka 의도·보고)
- `src/DeviceDetail.tsx` — 오른쪽 승인 패널·설정 패널·상세 + PING / 삭제 + 텔레메트리·이벤트 이력
- `src/Profiles.tsx` — 프로필 탭(표 안 편집·추가·삭제)
- `src/System.tsx` — 시스템 탭(/health, /api/metrics JSON)
