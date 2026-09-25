// 백엔드 REST 호출. docs/05_API.md 기준. fetch 만 쓴다.

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

export interface Device {
  uuid: string;
  state: string;
  state_reason: string | null;
  has_mqtt_account: boolean;
  fw: string | null;
  device_model: string | null;
  modem_model: string | null;
  imei: string | null;
  iccid: string | null;
  msisdn: string | null;
  cv_device: number | null;
  ss_device: number | null;
  ti_device: number | null;
  cv_server: number;
  ti_server: number;
  lat: number | null;
  lon: number | null;
  site: string | null;
  grp0: string | null;
  grp1: string | null;
  last_register_at: string | null;
  last_telemetry_at: string | null;
  last_seen_at: string | null;
  last_sq: number | null;
  online: boolean;
  is_online: boolean;
  offline_at: string | null;
  lost_count: number;
  reboot_count: number;
  config_sent_at: string | null;
  config_pending: boolean;
  created_at: string;
  updated_at: string;
  last_telemetry?: Telemetry | null;
}

export interface DeviceList {
  items: Device[];
  total: number;
  page: number;
  size: number;
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
  env: string;
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
  health: () => request<Health>("/health"),
  listDevices: (p: { page?: number; size?: number; state?: string; online?: string }) => {
    const q = new URLSearchParams();
    if (p.page) q.set("page", String(p.page));
    if (p.size) q.set("size", String(p.size));
    if (p.state) q.set("state", p.state);
    if (p.online) q.set("online", p.online);
    return request<DeviceList>(`/api/devices?${q}`);
  },
  getDevice: (uuid: string) => request<Device>(`/api/devices/${uuid}`),
  telemetry: (uuid: string, limit = 50) =>
    request<Telemetry[]>(`/api/devices/${uuid}/telemetry?limit=${limit}`),
  events: (uuid: string, limit = 50, kind?: string) =>
    request<DeviceEvent[]>(
      `/api/devices/${uuid}/events?limit=${limit}${kind ? `&kind=${kind}` : ""}`,
    ),
  patchConfig: (uuid: string, body: { ti?: number; lat?: number; lon?: number; site?: string }) =>
    request<unknown>(`/api/devices/${uuid}/config`, json("PATCH", body)),
  ping: (uuid: string) => request<{ uuid: string; seq: number }>(`/api/devices/${uuid}/ping`, json("POST")),
  deleteDevice: (uuid: string) => request<unknown>(`/api/devices/${uuid}`, json("DELETE")),
  importAccounts: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<unknown>("/api/devices/import-accounts", { method: "POST", body: fd });
  },
};

export function errorText(e: unknown): string {
  if (e instanceof ApiErrorException) {
    const d = e.err.detail ? ` ${JSON.stringify(e.err.detail)}` : "";
    return `[${e.status}] ${e.err.code}: ${e.err.message}${d}`;
  }
  return e instanceof Error ? e.message : String(e);
}
