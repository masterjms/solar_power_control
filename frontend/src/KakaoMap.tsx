// 카카오 지도(문제점 #3). JavaScript 키는 GET /api/ui-config 로 받는다(서버 .env KAKAO_JS_KEY).
// 키가 없거나 카카오 콘솔에 이 사이트 도메인이 등록되지 않았으면 지도 자리에 이유만 보여 준다.
// 두 가지로 쓴다: 단말 핀 지도(대시보드, 군집), 위치 보정(승인 화면, 끌 수 있는 핀 하나).
import { useEffect, useRef, useState } from "react";
import { api, errorText, MapPoint } from "./api";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare global {
  interface Window {
    kakao?: any;
  }
}

const SEOUL = { lat: 37.5665, lon: 126.978 };

let sdkPromise: Promise<any> | null = null;

/** SDK 를 한 번만 불러온다. 실패하면 다음 호출 때 다시 시도한다. */
function loadSdk(): Promise<any> {
  if (window.kakao?.maps?.LatLng) return Promise.resolve(window.kakao);
  if (sdkPromise) return sdkPromise;
  sdkPromise = api.uiConfig().then(
    (cfg) =>
      new Promise((resolve, reject) => {
        if (!cfg.kakao_js_key) {
          reject(new Error("카카오 JavaScript 키가 없습니다 — 서버 .env 의 KAKAO_JS_KEY"));
          return;
        }
        const s = document.createElement("script");
        s.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${encodeURIComponent(cfg.kakao_js_key)}&autoload=false&libraries=clusterer`;
        s.async = true;
        s.onload = () => {
          if (!window.kakao?.maps) {
            reject(new Error("카카오 지도 SDK 를 읽지 못했습니다"));
            return;
          }
          window.kakao.maps.load(() => resolve(window.kakao));
        };
        s.onerror = () =>
          reject(new Error("카카오 지도 SDK 를 받지 못했습니다 — 키가 맞는지, 카카오 콘솔 [플랫폼 > Web] 에 이 사이트 주소가 등록됐는지 확인"));
        document.head.appendChild(s);
      }),
  );
  sdkPromise.catch(() => {
    sdkPromise = null;
  });
  return sdkPromise;
}

function useKakao(): { kakao: any | null; error: string | null } {
  const [kakao, setKakao] = useState<any | null>(window.kakao?.maps?.LatLng ? window.kakao : null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (kakao) return;
    let alive = true;
    loadSdk().then((k) => alive && setKakao(k)).catch((e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [kakao]);
  return { kakao, error };
}

function MapNote({ text }: { text: string }) {
  return <div className="ph"><b>지도를 표시할 수 없습니다</b><span>{text}</span></div>;
}

const pinColor = (p: MapPoint) =>
  p.state === "PENDING" ? "#3b82f6" : !p.is_online ? "#8a94a6" : p.on === 1 ? "#f5b400" : "#22a06b";

function pinImage(kakao: any, color: string) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18"><circle cx="9" cy="9" r="7" fill="${color}" stroke="#fff" stroke-width="2"/></svg>`;
  return new kakao.maps.MarkerImage(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`,
    new kakao.maps.Size(18, 18), { offset: new kakao.maps.Point(9, 9) });
}

/** 단말 핀 지도 — 색: 점등(노랑)·소등(초록)·오프라인(회색)·승인 대기(파랑). 가까운 핀은 묶는다. */
export function DeviceMap({ points, onSelect, height = 420 }: { points: MapPoint[]; onSelect: (uuid: string) => void; height?: number }) {
  const { kakao, error } = useKakao();
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<any>(null);
  const cluster = useRef<any>(null);
  const fitted = useRef(false);

  useEffect(() => {
    if (!kakao || !box.current || map.current) return;
    map.current = new kakao.maps.Map(box.current, { center: new kakao.maps.LatLng(SEOUL.lat, SEOUL.lon), level: 9 });
    map.current.addControl(new kakao.maps.ZoomControl(), kakao.maps.ControlPosition.RIGHT);
    cluster.current = new kakao.maps.MarkerClusterer({ map: map.current, averageCenter: true, minLevel: 7 });
  }, [kakao]);

  useEffect(() => {
    if (!kakao || !map.current) return;
    const images = new Map<string, any>();
    const markers = points.map((p) => {
      const c = pinColor(p);
      if (!images.has(c)) images.set(c, pinImage(kakao, c));
      const m = new kakao.maps.Marker({ position: new kakao.maps.LatLng(p.lat, p.lon), image: images.get(c), title: `${p.site ?? p.uuid}${p.node_name ? ` · ${p.node_name}` : ""}` });
      kakao.maps.event.addListener(m, "click", () => onSelect(p.uuid));
      return m;
    });
    cluster.current.clear();
    cluster.current.addMarkers(markers);
    if (!fitted.current && points.length) {
      const b = new kakao.maps.LatLngBounds();
      points.forEach((p) => b.extend(new kakao.maps.LatLng(p.lat, p.lon)));
      map.current.setBounds(b);
      fitted.current = true;
    }
  }, [kakao, points, onSelect]);

  if (error) return <MapNote text={error} />;
  return <div ref={box} className="kmap" style={{ height }}>{!kakao && <div className="muted" style={{ padding: 12 }}>지도 불러오는 중…</div>}</div>;
}

/** 위치 보정 — 끌 수 있는 핀 하나. 지도를 눌러도 핀이 옮겨진다. 옮길 때마다 onMove(lat, lon). */
export function LocationPicker({ lat, lon, onMove, height = 260 }: {
  lat: number | null; lon: number | null; onMove: (lat: number, lon: number) => void; height?: number;
}) {
  const { kakao, error } = useKakao();
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<any>(null);
  const marker = useRef<any>(null);
  const move = useRef(onMove);
  move.current = onMove;

  useEffect(() => {
    if (!kakao || !box.current || map.current) return;
    const at = new kakao.maps.LatLng(lat ?? SEOUL.lat, lon ?? SEOUL.lon);
    map.current = new kakao.maps.Map(box.current, { center: at, level: lat === null ? 8 : 3 });
    map.current.addControl(new kakao.maps.ZoomControl(), kakao.maps.ControlPosition.RIGHT);
    marker.current = new kakao.maps.Marker({ position: at, draggable: true, map: map.current });
    const fire = (p: any) => move.current(Number(p.getLat().toFixed(6)), Number(p.getLng().toFixed(6)));
    kakao.maps.event.addListener(marker.current, "dragend", () => fire(marker.current.getPosition()));
    kakao.maps.event.addListener(map.current, "click", (e: any) => {
      marker.current.setPosition(e.latLng);
      fire(e.latLng);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kakao]);

  // 밖에서 좌표가 바뀌면(주소 검색) 핀과 지도를 옮긴다
  useEffect(() => {
    if (!kakao || !marker.current || lat === null || lon === null) return;
    const cur = marker.current.getPosition();
    if (Math.abs(cur.getLat() - lat) < 1e-7 && Math.abs(cur.getLng() - lon) < 1e-7) return;
    const at = new kakao.maps.LatLng(lat, lon);
    marker.current.setPosition(at);
    map.current.setCenter(at);
    if (map.current.getLevel() > 4) map.current.setLevel(3);
  }, [kakao, lat, lon]);

  if (error) return <MapNote text={error} />;
  return <div ref={box} className="kmap" style={{ height }}>{!kakao && <div className="muted" style={{ padding: 12 }}>지도 불러오는 중…</div>}</div>;
}
