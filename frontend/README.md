# frontend — 개발용 최소 화면

2차 REST API(`docs/05_API.md`)를 눈으로 확인하기 위한 화면. 예쁠 필요 없음, 기능만.
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
- `src/App.tsx` — 상단 요약바(집계 + /health), 탭(#devices / #admin), 좌우 분할
- `src/DeviceList.tsx` — 왼쪽 단말 목록(필터·페이지)
- `src/DeviceDetail.tsx` — 오른쪽 상세 + 설정 변경 / PING / 삭제 + 텔레메트리·이벤트 이력
- `src/ImportAccounts.tsx` — 관리 탭: 계정 CSV import
