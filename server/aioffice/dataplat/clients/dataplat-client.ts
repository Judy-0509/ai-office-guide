// Typed fetch client for the dataplat HTTP API (server/aioffice/dataplat/server.py).
// Framework-agnostic: only uses `fetch`/`URLSearchParams`, no React/Vue/etc dependency, and no
// chart library choice is made here -- `ChatResponse.chart` is plain data
// ({type, x, series}); render it with whatever the dashboard already uses.
//
// Usage: `new DataplatClient("")` when the client runs on the same origin as the server (the
// normal deployment -- `dataplat.server` serves the dashboard build AND the API together, so
// no CORS setup is needed); pass a full origin ("http://localhost:8000") only for the Vite dev
// server, matching `--cors` on `dataplat.server`.
//
// See server/README.md's "데이터 플랫폼 대시보드 연동" section for a React usage example.

export interface DatasetCatalog {
  name: string;
  title: string;
  metrics: string[];
  entities: string[];
  regions: string[];
  sources: string[];
  units: string[];
  period_from: string | null;
  period_to: string | null;
  last_load: { id: number; status: string; started_at: string; finished_at: string | null } | null;
}

export interface ObservationRow {
  metric: string;
  entity: string;
  region: string;
  period: string;
  period_sort: string;
  source: string;
  value: number | null;
  unit: string;
}

export interface WideResult {
  columns: string[];
  rows: (string | number | null)[][];
}

export interface HistoryPoint {
  load_id: number;
  started_at: string;
  finished_at: string | null;
  value: number | null;
  unit: string;
  region: string;
  source: string;
}

export interface LoadSummary {
  id: number;
  dataset: string;
  content_hash: string;
  rows: number;
  status: "ok" | "error" | "skipped_duplicate";
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface LoadDetail extends LoadSummary {
  report: Record<string, unknown> | null;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatSeries {
  name: string;
  values: (number | null)[];
}

export interface ChatChart {
  type: "line" | "bar" | "stacked" | "table";
  x: (string | number)[];
  series: ChatSeries[];
  unit: string;
}

export interface ChatResponse {
  answer: string;
  spec: Record<string, unknown> | null;
  table: WideResult | null;
  chart: ChatChart | null;
  warnings: string[];
  seconds: number;
}

export interface RefreshDatasetResult {
  dataset: string;
  status: "ok" | "error" | "skipped_duplicate";
  rows: number;
  error?: string;
}

export interface RefreshResult {
  ok: boolean;
  stage: "pre_command" | "snapshot" | "unexpected";
  results: RefreshDatasetResult[];
}

export interface QueryLongParams {
  dataset: string;
  metric?: string[];
  entity?: string[];
  region?: string[];
  source?: string[];
  period_from?: string;
  period_to?: string;
  version?: "latest" | "all" | number;
}

export interface QueryWideParams extends QueryLongParams {
  format: "wide";
  rows?: string[]; // row dimensions, e.g. ["entity"]
  cols?: string[]; // column dimensions, e.g. ["period"]
}

function buildQuery(params: Record<string, string | undefined>): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") usp.set(key, value);
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export class DataplatClient {
  constructor(private baseUrl: string = "", private adminToken?: string) {}

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...((init?.headers as Record<string, string>) || {}),
    };
    if (this.adminToken) headers["Authorization"] = `Bearer ${this.adminToken}`;
    const res = await fetch(`${this.baseUrl}${path}`, { ...init, headers });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error((body && body.error) || `요청 실패: ${res.status}`);
    }
    return body as T;
  }

  catalog(): Promise<{ datasets: DatasetCatalog[] }> {
    return this.request(`/api/catalog`);
  }

  queryLong(params: QueryLongParams): Promise<{ rows: ObservationRow[] }> {
    return this.request(`/api/query${this._queryString(params)}`);
  }

  queryWide(params: QueryWideParams): Promise<WideResult> {
    return this.request(`/api/query${this._queryString(params)}`);
  }

  private _queryString(params: QueryLongParams & Partial<QueryWideParams>): string {
    return buildQuery({
      dataset: params.dataset,
      metric: params.metric?.join(","),
      entity: params.entity?.join(","),
      region: params.region?.join(","),
      source: params.source?.join(","),
      period_from: params.period_from,
      period_to: params.period_to,
      version: params.version !== undefined ? String(params.version) : undefined,
      format: params.format,
      rows: params.rows?.join(","),
      cols: params.cols?.join(","),
    });
  }

  history(params: {
    dataset: string;
    metric: string;
    entity: string;
    period: string;
    source?: string;
  }): Promise<{ history: HistoryPoint[] }> {
    return this.request(`/api/history${buildQuery(params)}`);
  }

  loads(dataset?: string): Promise<{ loads: LoadSummary[] }> {
    return this.request(`/api/loads${buildQuery({ dataset })}`);
  }

  load(id: number): Promise<LoadDetail> {
    return this.request(`/api/loads/${id}`);
  }

  refreshStatus(): Promise<{ running: boolean; last_result: RefreshResult | null }> {
    return this.request(`/api/refresh/status`);
  }

  // Requires an admin token whenever the server isn't bound to loopback -- see
  // `dataplat.server`'s --admin-token.
  refresh(dataset?: string): Promise<{ ok: true }> {
    return this.request(`/api/refresh`, { method: "POST", body: JSON.stringify({ dataset }) });
  }

  chat(message: string, history: ChatMessage[] = []): Promise<ChatResponse> {
    return this.request(`/api/chat`, {
      method: "POST",
      body: JSON.stringify({ message, history }),
    });
  }
}
