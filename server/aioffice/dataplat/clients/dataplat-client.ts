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

// A clarify-button choice: `spec_patch` runs instantly via `DataplatClient.chatWithSpec`
// (`path: "button"`, no LLM); `action: "ask_ai"` (only ever the LAST option) instead queues an
// async job via `DataplatClient.ask` -- the only way this chat ever reaches the LLM now.
export interface ChatOption {
  label: string;
  spec_patch?: Record<string, unknown>;
  action?: "ask_ai";
}

export interface ChatResponse {
  answer: string;
  spec: Record<string, unknown> | null;
  table: WideResult | null;
  chart: ChatChart | null;
  warnings: string[];
  seconds: number;
  // How the query spec was resolved: "rule"/"cache"/"button" never call the LLM; "llm" only
  // happens inside an async /api/ask job now (never from a plain chat() call) -- "none" means
  // neither the rule path nor the cache resolved it, so `options` (below) has clickable
  // suggestions plus an "AI에게 물어보기" entry. Show a "사내 LLM 응답 대기 중..." elapsed-time
  // indicator only while an /api/ask job's status is "running".
  path: "rule" | "cache" | "button" | "llm" | "none";
  timings: ChatTimings;
  // Present (non-null) only when `path === "none"` -- see `ChatOption`.
  options: ChatOption[] | null;
}

export interface SuggestionOption {
  label: string;
  spec_patch: Record<string, unknown>;
}

export interface DigestEntry {
  id: number;
  dataset: string;
  load_id: number;
  kind: "version_summary" | "load_summary" | "entity_note";
  title: string;
  text: string;
  table: WideResult;
  created_at: string;
}

export interface LearnedAlias {
  id: number;
  dataset: string;
  dim: "entities" | "regions" | "metrics";
  phrase: string;
  values: string[];
  hit_count: number;
  created_at: string;
}

export interface AliasesResponse {
  // aliases.yaml's raw content, keyed by dataset -- see dataplat/aliases.py's module docstring.
  aliases: Record<string, unknown>;
  learned: LearnedAlias[];
}

export interface AskJobStatus {
  id: number;
  message: string;
  history: ChatMessage[];
  status: "queued" | "running" | "done" | "error" | "cancelled";
  result: ChatResponse | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  position: number; // 1-based position among still-queued jobs; 0 once it's left the queue
  elapsed_seconds: number;
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

  // Never calls the LLM -- resolves via the rule path or the spec cache, or returns `options`
  // (path: "none") for the caller to render as buttons. `explain`: "template" (default
  // server-side -- instant, no LLM call) or "llm" (one extra call for a nicer sentence; only
  // meaningful once a spec was already resolved, so it still never triggers the automatic LLM
  // spec call this method itself never makes).
  chat(message: string, history: ChatMessage[] = [],
       explain?: "llm" | "template"): Promise<ChatResponse> {
    return this.request(`/api/chat`, {
      method: "POST",
      body: JSON.stringify({ message, history, explain }),
    });
  }

  // Runs a clarify-button/suggestion's `spec_patch` directly (`path: "button"`, no LLM).
  // `originalMessage`, when the button came from a real question (not a plain suggestion
  // click), lets the server's learning loop remember it for next time.
  chatWithSpec(specPatch: Record<string, unknown>, originalMessage?: string,
               explain?: "llm" | "template"): Promise<ChatResponse> {
    return this.request(`/api/chat`, {
      method: "POST",
      body: JSON.stringify({ spec_patch: specPatch, message: originalMessage, explain }),
    });
  }

  // 6-8 ready-to-click questions for `dataset`, each running instantly via `chatWithSpec`.
  suggestions(dataset: string): Promise<{ suggestions: SuggestionOption[] }> {
    return this.request(`/api/suggestions${buildQuery({ dataset })}`);
  }

  // The latest batch-generated summary for `dataset` (dataplat.digest, run after a snapshot) --
  // [] if it hasn't run yet for this dataset.
  digest(dataset: string): Promise<{ digests: DigestEntry[] }> {
    return this.request(`/api/digest${buildQuery({ dataset })}`);
  }

  aliases(): Promise<AliasesResponse> {
    return this.request(`/api/aliases`);
  }

  // Requires an admin token whenever the server isn't bound to loopback (same rule as refresh()).
  deleteLearnedAlias(id: number): Promise<{ ok: true }> {
    return this.request(`/api/aliases/learned/${id}`, { method: "DELETE" });
  }

  // Queues an "AI에게 물어보기" job and returns immediately -- poll askStatus(job_id) (or watch
  // askRecent()) for the result. Requires an admin token whenever the server isn't bound to
  // loopback (this is the only way the chat ever reaches the LLM, same protection as refresh()).
  ask(message: string, history: ChatMessage[] = []): Promise<{ job_id: number }> {
    return this.request(`/api/ask`, { method: "POST", body: JSON.stringify({ message, history }) });
  }

  askStatus(jobId: number): Promise<AskJobStatus> {
    return this.request(`/api/ask/${jobId}`);
  }

  askRecent(limit = 20): Promise<{ jobs: AskJobStatus[] }> {
    return this.request(`/api/ask${buildQuery({ recent: String(limit) })}`);
  }

  // Queued: removed, never runs. Running: discarded once its (already in-flight) LLM call
  // returns. Requires an admin token whenever the server isn't bound to loopback.
  cancelAsk(jobId: number): Promise<{ ok: true }> {
    return this.request(`/api/ask/${jobId}`, { method: "DELETE" });
  }
}
