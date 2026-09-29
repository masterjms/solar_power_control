// 카카오 지도(문제점 #3). JavaScript 키는 GET /api/ui-config 로 받는다(서버 .env KAKAO_JS_KEY).
// 키가 없거나 카카오 콘솔에 이 사이트 도메인이 등록되지 않았으면 지도 자리에 이유만 보여 준다.
// 두 가지로 쓴다: 단말 핀 지도(대시보드, 군집), 위치 보정(승인 화면, 끌 수 있는 핀 하나).
import { useEffect, useRef, useState } from "react";
import { api, errorText, MapPoint } from "./api";
import { volt1 } from "./format";

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

/**
 * 핀 모양(문제점 13번). 색 = 통신, 속 = 조명 — 초록이면 붙어 있고, 속이 차 있으면 켜져 있다.
 *   오프라인 회색 · 온라인+점등 녹색(찬 원) · 온라인+소등 녹색 테두리(빈 원) · 승인 대기 파랑.
 * 소등을 다른 색으로 두지 않은 이유: 낮에는 거의 모든 단말이 소등이라 그 색이 지도를 덮는다.
 * 경고색(노랑·빨강)이면 이상처럼 보이고, 회색 계열이면 오프라인과 헷갈린다.
 */
export const PIN = {
  offline: { fill: "#8a94a6", ring: "#ffffff", label: "오프라인" },
  lit: { fill: "#22a06b", ring: "#ffffff", label: "온라인 · 점등" },
  dark: { fill: "#ffffff", ring: "#22a06b", label: "온라인 · 소등" },
  pending: { fill: "#3b82f6", ring: "#ffffff", label: "승인 대기" },
} as const;
type PinKind = keyof typeof PIN;

const pinKind = (p: MapPoint): PinKind =>
  p.state === "PENDING" ? "pending" : !p.is_online ? "offline" : (p.lit ?? p.on === 1) ? "lit" : "dark";

function pinImage(kakao: any, kind: PinKind) {
  const { fill, ring } = PIN[kind];
  const w = kind === "dark" ? 3 : 2;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18"><circle cx="9" cy="9" r="${9 - w / 2 - 0.5}" fill="${fill}" stroke="${ring}" stroke-width="${w}"/></svg>`;
  return new kakao.maps.MarkerImage(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`,
    new kakao.maps.Size(18, 18), { offset: new kakao.maps.Point(9, 9) });
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/** 마우스를 올리면 뜨는 말풍선 — 시설명, 배터리 전압. 오프라인이면 "마지막 값"이라고 적는다. */
function hoverHtml(p: MapPoint): string {
  const v = volt1(p.bv);
  const volt = v ? `${v}${p.is_online ? "" : " (마지막 값)"}` : "배터리 전압 없음";
  return `<div class="kpin-tip"><b>${esc(p.site ?? p.uuid)}</b><span>${esc(volt)}</span></div>`;
}

/** 단말 핀 지도 — 색은 PIN. 가까운 핀은 묶는다. 핀에 마우스를 올리면 시설명·배터리 전압. */
export function DeviceMap({ points, onSelect, height = 420 }: { points: MapPoint[]; onSelect: (uuid: string) => void; height?: number }) {
  const { kakao, error } = useKakao();
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<any>(null);
  const cluster = useRef<any>(null);
  const tip = useRef<any>(null);
  const fitted = useRef(false);

  useEffect(() => {
    if (!kakao || !box.current || map.current) return;
    map.current = new kakao.maps.Map(box.current, { center: new kakao.maps.LatLng(SEOUL.lat, SEOUL.lon), level: 9 });
    map.current.addControl(new kakao.maps.ZoomControl(), kakao.maps.ControlPosition.RIGHT);
    cluster.current = new kakao.maps.MarkerClusterer({ map: map.current, averageCenter: true, minLevel: 7 });
    tip.current = new kakao.maps.CustomOverlay({ yAnchor: 1.6, zIndex: 3 });
  }, [kakao]);

  useEffect(() => {
    if (!kakao || !map.current) return;
    const images = new Map<PinKind, any>();
    const markers = points.map((p) => {
      const k = pinKind(p);
      if (!images.has(k)) images.set(k, pinImage(kakao, k));
      const pos = new kakao.maps.LatLng(p.lat, p.lon);
      const m = new kakao.maps.Marker({ position: pos, image: images.get(k) });
      kakao.maps.event.addListener(m, "click", () => onSelect(p.uuid));
      kakao.maps.event.addListener(m, "mouseover", () => {
        tip.current.setContent(hoverHtml(p));
        tip.current.setPosition(pos);
        tip.current.setMap(map.current);
      });
      kakao.maps.event.addListener(m, "mouseout", () => tip.current.setMap(null));
      return m;
    });
    tip.current?.setMap(null);
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
