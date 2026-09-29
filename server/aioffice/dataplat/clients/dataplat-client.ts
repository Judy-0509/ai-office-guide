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
  // Non-empty only for a dataset whose source.yaml sets version_column (e.g. forecast
  // vintages "2026-07"/"2026-08"), oldest to newest.
  version_labels: string[];
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
  // "" unless the dataset uses version_column, in which case this is the version label
  // ("2026-07") and there's one point per label (its latest value) instead of one per snapshot.
  version_label: string;
  started_at: string;
  finished_at: string | null;
  value: number | null;
  unit: string;
  region: string;
  source: string;
}

export interface VersionDiffRow {
  // one key per `group_by` dimension (e.g. `entity: "모델B"` when group_by is ["entity"]),
  // plus the four computed columns below.
  [dimensionOrColumn: string]: string | number | null;
  old: number | null; // null if this key didn't exist in `from_label` (an added key)
  new: number | null; // null if this key didn't exist in `to_label` (a removed key)
  diff: number;
  pct: number | null; // a PERCENT NUMBER (25 means +25%), null when old is 0 or absent
}

export interface VersionDiffResult {
  dataset: string;
  from_label: string;
  to_label: string;
  group_by: string[];
  sort: "abs" | "rel";
  totals: { changed: number; added: number; removed: number; total_keys: number };
  rows: VersionDiffRow[];
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

export interface ChatTimings {
  spec_seconds: number;
  explain_seconds: number;
  query_seconds: number;
}

export interface ChatResponse {
  answer: string;
  spec: Record<string, unknown> | null;
  table: WideResult | null;
  chart: ChatChart | null;
  warnings: string[];
  seconds: number;
  // How the query spec was resolved: "rule" (deterministic, no LLM call), "cache" (a previous
  // identical question, no LLM call), or "llm" (a spec call was made). Only "llm" can ever be
  // slow (the shared in-house endpoint's queue) -- show a "사내 LLM 응답 대기 중..." elapsed-time
  // indicator only when `path === "llm"`.
  path: "rule" | "cache" | "llm";
  timings: ChatTimings;
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
  // Group periods to a coarser level before returning rows (e.g. quarters -> one point per
  // year), useful for "OO년 합계" style questions. `agg` defaults to "sum" server-side.
  rollup?: "year" | "quarter";
  agg?: "sum" | "avg";
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
      rollup: params.rollup,
      agg: params.agg,
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

  // Compares two versions of a dataset (defaults: to = latest label, from = the label right
  // before it), aggregated by `group_by` (default ["entity"]) so e.g. "which model changed
  // the most" ranks correctly across periods. `sort`: "abs" (largest |diff|, default) or "rel"
  // (largest % change).
  versionDiff(params: {
    dataset: string;
    from?: string;
    to?: string;
    metric?: string;
    entity?: string;
    region?: string;
    source?: string;
    period_from?: string;
    period_to?: string;
    group_by?: string[];
    top?: number;
    sort?: "abs" | "rel";
  }): Promise<VersionDiffResult> {
    return this.request(`/api/version_diff${buildQuery({
      dataset: params.dataset, from: params.from, to: params.to, metric: params.metric,
      entity: params.entity, region: params.region, source: params.source,
      period_from: params.period_from, period_to: params.period_to,
      group_by: params.group_by?.join(","),
      top: params.top !== undefined ? String(params.top) : undefined, sort: params.sort,
    })}`);
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

  // `explain`: "template" (default server-side -- instant, no LLM call) or "llm" (one extra
  // call for a nicer sentence; slower on a busy shared endpoint).
  chat(message: string, history: ChatMessage[] = [],
       explain?: "llm" | "template"): Promise<ChatResponse> {
    return this.request(`/api/chat`, {
      method: "POST",
      body: JSON.stringify({ message, history, explain }),
    });
  }
}
