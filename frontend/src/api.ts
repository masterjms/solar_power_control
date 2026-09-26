// 백엔드 REST 호출. docs/05_API.md (2026-09-26 개정) 기준. fetch 만 쓴다.

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
  listDevices: (p: { page?: number; size?: number; state?: string; online?: string; q?: string }) => {
    const q = new URLSearchParams();
    if (p.page) q.set("page", String(p.page));
    if (p.size) q.set("size", String(p.size));
    if (p.state) q.set("state", p.state);
    if (p.online) q.set("online", p.online);
    if (p.q) q.set("q", p.q);
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
};

export function errorText(e: unknown): string {
  if (e instanceof ApiErrorException) {
    const d = e.err.detail ? ` ${JSON.stringify(e.err.detail)}` : "";
    return `[${e.status}] ${e.err.code}: ${e.err.message}${d}`;
  }
  return e instanceof Error ? e.message : String(e);
}
