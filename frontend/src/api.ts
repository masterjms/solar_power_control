// 백엔드 REST 호출. docs/05_API.md (2026-09-26 개정 + 2026-09-27 "5차 API") 기준. fetch 만 쓴다.

export interface ApiError {
  code: string;
  message: string;
  detail?: unknown;
}

export class ApiErrorException extends Error {
  status: number;
  err: ApiError;
  constructor(status: number, err: ApiError) {
    super(`${err.code}: ${err.message}`);
    this.status = status;
    this.err = err;
  }
}

export interface Telemetry {
  received_at?: string;
  ts_device?: string;
  ts?: string;
  sq?: number;
  fw?: string;
  ss?: number;
  cv?: number;
  er?: number;
  on?: number;
  md?: number;
  pw?: number[];
  bv?: number;
  bi?: number;
  sc?: number;
  pp?: number;
  li?: number;
  cs?: number;
}

export type DeviceState = "PENDING" | "ACTIVE" | "SUSPENDED" | "REJECTED" | "RETIRED";
export const STATES: DeviceState[] = ["PENDING", "ACTIVE", "SUSPENDED", "REJECTED", "RETIRED"];

/** DeviceOut = device 컬럼 전부(docs/03) + 계산 필드(docs/05). */
export interface Device {
  uuid: string;
  state: DeviceState;
  state_reason: string | null;
  state_changed_at: string | null;
  register_ack_at: string | null;
  fw: string | null;
  device_model: string | null;
  modem_model: string | null;
  imei: string | null;
  iccid: string | null;
  msisdn: string | null;
  cv_device: number | null;
  ss_device: number | null;
  ti_device: number | null;
  ka_device: number | null;
  cv_server: number;
  profile_id: number;
  profile_name: string | null;
  ti_override: number | null;
  ka_override: number | null;
  ti_effective: number;
  ka_effective: number;
  lat: number | null;
  lon: number | null;
  site: string | null;
  address: string | null;
  bjd_code: string | null;
  grp: string | null;
  last_register_at: string | null;
  last_telemetry_at: string | null;
  last_seen_at: string | null;
  last_sq: number | null;
  last_telemetry: Telemetry | null;
  online: boolean;
  online_changed_at: string | null;
  offline_at: string | null;
  lost_count: number;
  reboot_count: number;
  config_sent_at: string | null;
  created_at: string;
  updated_at: string;
  // 계산 필드
  config_pending: boolean;
  config_mismatch: boolean;
  is_online: boolean;
  // 5차 (docs/05 "단말 배정")
  node_id: number | null;
  node_name: string | null;
  node_path: string | null; // "경기도 > 안양시 만안구 > 안양동"
  override_act: CmdAct | null;
  override_level: string | null;
  override_seq: number | null;
  override_until: string | null;
  remote_active: boolean; // last_telemetry.md == 2 AND override_until > now
  remote_remaining_sec: number | null;
}

export interface DeviceCounts {
  PENDING: number;
  ACTIVE: number;
  SUSPENDED: number;
  REJECTED: number;
  RETIRED: number;
  online: number;
}

export interface DeviceList {
  items: Device[];
  total: number;
  page: number;
  size: number;
  counts: DeviceCounts;
}

export interface DeviceEvent {
  id: number;
  kind: string;
  payload: unknown;
  received_at: string;
}

export interface Health {
  ok: boolean;
  mqtt_connected: boolean;
  db_ok: boolean;
  buffer_pending: number;
  register_queue: number;
  telemetry_dropped: number;
  flush_failures: number;
  broker_log_tail: boolean;
  hmac_keys: string[];
  test_account_enabled: boolean;
  env: string;
}

export interface Profile {
  id: number;
  name: string;
  ti: number;
  ka: number;
  device_count: number;
}

export interface ProfilePatchRes extends Profile {
  bumped_devices?: number;
}

export interface StatePatchBody {
  state: DeviceState;
  site?: string;
  reason?: string | null;
  node_id?: number; // 5차: ACTIVE 로 갈 때 말단 배정(APPROVE_REQUIRES_NODE)
}

export interface StatePatchRes {
  uuid: string;
  state: DeviceState;
  site: string | null;
  published: boolean;
  register_ack: unknown;
}

export interface ConfigPatchBody {
  profile_id?: number;
  ti_override?: number | null;
  ka_override?: number | null;
  lat?: number | null;
  lon?: number | null;
  site?: string;
  address?: string | null;
  bjd_code?: string | null;
  node_id?: number | null; // 5차: 말단만(NODE_NOT_LEAF). null = 배정 해제
}

export interface ConfigPatchRes {
  uuid: string;
  cv_server: number;
  ti_effective: number;
  ka_effective: number;
  lat: number | null;
  lon: number | null;
  site: string | null;
  published: boolean;
  reason?: string; // published=false 일 때 "NOT_ACTIVE" 등
  payload: unknown;
  register_ack_republished?: boolean;
}

// ---------------- 5차: 권한 · 법정동 트리 · 명령 ----------------

export type Role = "super_admin" | "admin";
export interface Me {
  user: string;
  role: Role;
}

export type RegionLevel = "sido" | "sigungu" | "dong";
/** GET /api/regions 평면 목록 한 줄. 말단(dong)만 bjd_code·grp 를 가진다. */
export interface Region {
  id: number;
  parent_id: number | null;
  level: RegionLevel;
  name: string;
  bjd_code: string | null;
  grp: string | null;
  lat: number | null;
  lon: number | null;
  device_count: number; // 자신 포함 하위 전체
  active_count: number;
}

/** GET /api/geo/search 결과 한 줄(카카오 로컬 프록시). */
export interface GeoResult {
  address_name: string;
  bjd_code: string;
  sido: string;
  sigungu: string;
  dong: string;
  lat: number | null;
  lon: number | null;
}

/** POST /api/regions/from-address 본문 — 검색어 / 고른 결과 / (dev) 직접 입력. */
export type FromAddressBody =
  | { query: string }
  | { pick: GeoResult }
  | { sido: string; sigungu: string; dong: string; bjd_code: string; lat?: number; lon?: number };

export type CmdAct = "on" | "off" | "pwm" | "auto";
export type DurPreset = "30m" | "1h" | "3h" | "tonight";
export type TargetKind = "device" | "node" | "all";

export interface CommandTargetRef {
  kind: TargetKind;
  id: string | null; // device = uuid, node = region id(문자열), all = null
}

export interface CommandBody {
  target: CommandTargetRef;
  act: CmdAct;
  ch: number[];
  pwm?: number[];
  dur?: number | null;
  dur_preset?: DurPreset | null;
  exp?: number;
}

export interface CommandPreview {
  expected: number;
  online: number;
  offline: number;
  low_battery: number;
  not_active: number;
  topics: string[];
  dur: number | null;
  payload: Record<string, unknown>;
}

export interface CommandCreated {
  seq: number;
  target: { kind: TargetKind; id: string | null; label: string };
  topics: string[];
  payload: Record<string, unknown>;
  expected: number;
  sent_at: string;
  created_by: string;
}

export type AckStatus = "OK" | "LOCAL" | "EXPIRED" | "BAD" | "STATE" | "pending";
export const ACK_STATUSES: AckStatus[] = ["OK", "LOCAL", "EXPIRED", "BAD", "STATE", "pending"];

export interface CommandSummary {
  seq: number;
  created_by: string;
  target_kind: TargetKind;
  target_id: string | null;
  target_label: string | null;
  act: CmdAct;
  ch: number[] | null;
  pwm: number[] | null;
  dur: number | null;
  sent_at: string;
  finished_at: string | null;
  result: string | null; // OK / PARTIAL / TIMEOUT, 진행 중이면 null
  expected_count: number;
  counts: Partial<Record<AckStatus, number>>;
}

export interface CommandTargetRow {
  uuid: string;
  site: string | null;
  node_name: string | null;
  status: AckStatus;
  attempts: number;
  last_sent_at: string | null;
  acked_at: string | null;
  is_online: boolean;
}

export interface CommandDetail extends CommandSummary {
  targets: CommandTargetRow[];
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const e = (body as { error?: ApiError } | null)?.error;
    throw new ApiErrorException(
      res.status,
      e ?? { code: `HTTP_${res.status}`, message: text || res.statusText },
    );
  }
  return body as T;
}

function json(method: string, data?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  };
}

export const api = {
  // 시스템
  health: () => request<Health>("/health"),
  metrics: () => request<Record<string, unknown>>("/api/metrics"),

  // 프로필
  listProfiles: () => request<Profile[]>("/api/profiles"),
  createProfile: (body: { name: string; ti: number; ka: number }) =>
    request<Profile>("/api/profiles", json("POST", body)),
  patchProfile: (id: number, body: { name?: string; ti?: number; ka?: number }) =>
    request<ProfilePatchRes>(`/api/profiles/${id}`, json("PATCH", body)),
  deleteProfile: (id: number) => request<unknown>(`/api/profiles/${id}`, json("DELETE")),

  // 단말
  listDevices: (p: {
    page?: number; size?: number; state?: string; online?: string; q?: string;
    node_id?: number; remote?: boolean;
  }) => {
    const q = new URLSearchParams();
    if (p.page) q.set("page", String(p.page));
    if (p.size) q.set("size", String(p.size));
    if (p.state) q.set("state", p.state);
    if (p.online) q.set("online", p.online);
    if (p.q) q.set("q", p.q);
    if (p.node_id !== undefined) q.set("node_id", String(p.node_id));
    if (p.remote) q.set("remote", "true");
    return request<DeviceList>(`/api/devices?${q}`);
  },
  getDevice: (uuid: string) => request<Device>(`/api/devices/${uuid}`),
  telemetry: (uuid: string, limit = 50) =>
    request<Telemetry[]>(`/api/devices/${uuid}/telemetry?limit=${limit}`),
  events: (uuid: string, limit = 50, kind?: string) =>
    request<DeviceEvent[]>(
      `/api/devices/${uuid}/events?limit=${limit}${kind ? `&kind=${kind}` : ""}`,
    ),
  patchState: (uuid: string, body: StatePatchBody) =>
    request<StatePatchRes>(`/api/devices/${uuid}/state`, json("PATCH", body)),
  republishRegisterAck: (uuid: string) =>
    request<unknown>(`/api/devices/${uuid}/register-ack`, json("POST")),
  patchConfig: (uuid: string, body: ConfigPatchBody) =>
    request<ConfigPatchRes>(`/api/devices/${uuid}/config`, json("PATCH", body)),
  ping: (uuid: string) => request<{ uuid: string; seq: number }>(`/api/devices/${uuid}/ping`, json("POST")),
  deleteDevice: (uuid: string) => request<unknown>(`/api/devices/${uuid}`, json("DELETE")),

  // 5차: 권한
  me: () => request<Me>("/api/me"),

  // 5차: 법정동 트리
  listRegions: () => request<Region[]>("/api/regions"),
  geoSearch: (query: string) =>
    request<GeoResult[]>(`/api/geo/search?${new URLSearchParams({ query })}`),
  createRegionFromAddress: (body: FromAddressBody) =>
    request<Region>("/api/regions/from-address", json("POST", body)),
  patchRegion: (id: number, body: { name: string }) =>
    request<Region>(`/api/regions/${id}`, json("PATCH", body)),
  deleteRegion: (id: number) => request<unknown>(`/api/regions/${id}`, json("DELETE")),

  // 5차: 명령
  previewCommand: (body: CommandBody) =>
    request<CommandPreview>("/api/commands/preview", json("POST", body)),
  createCommand: (body: CommandBody) => request<CommandCreated>("/api/commands", json("POST", body)),
  listCommands: (p: { limit?: number; uuid?: string; node_id?: number } = {}) => {
    const q = new URLSearchParams();
    q.set("limit", String(p.limit ?? 50));
    if (p.uuid) q.set("uuid", p.uuid);
    if (p.node_id !== undefined) q.set("node_id", String(p.node_id));
    return request<CommandSummary[]>(`/api/commands?${q}`);
  },
  getCommand: (seq: number) => request<CommandDetail>(`/api/commands/${seq}`),
  retryCommand: (seq: number, uuids: string[] | null = null) =>
    request<{ resent: number }>(`/api/commands/${seq}/retry`, json("POST", { uuids })),
};

/** 5차 에러 코드 → 화면에 먼저 보일 한 줄(docs/05 "에러 코드 추가"). */
const ERROR_HINT: Record<string, string> = {
  FORBIDDEN: "최고관리자만 할 수 있습니다",
  GEO_UNAVAILABLE: "주소 검색(카카오 키)을 쓸 수 없습니다",
  REGION_IN_USE: "하위 지역이나 배정된 단말이 있어 지울 수 없습니다",
  NODE_NOT_LEAF: "말단 법정동만 고를 수 있습니다",
  NODE_REQUIRED: "승인하려면 말단 법정동을 골라야 합니다",
  REGION_NOT_FOUND: "지역이 없습니다(다른 사람이 지웠을 수 있음)",
  COMMAND_NOT_FOUND: "명령이 없습니다",
  COMMAND_FINISHED: "이미 끝난 명령이라 재시도할 수 없습니다",
  NO_TARGETS: "대상 범위에 운영(ACTIVE) 단말이 없습니다",
  MQTT_UNAVAILABLE: "브로커에 연결되어 있지 않아 보내지 못했습니다",
};

export function errorText(e: unknown): string {
  if (e instanceof ApiErrorException) {
    const d = e.err.detail ? ` ${JSON.stringify(e.err.detail)}` : "";
    const hint = ERROR_HINT[e.err.code];
    return `${hint ? `${hint} — ` : ""}[${e.status}] ${e.err.code}: ${e.err.message}${d}`;
  }
  return e instanceof Error ? e.message : String(e);
}
