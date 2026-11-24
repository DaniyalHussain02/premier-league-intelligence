import type {
  BacktestEntry,
  BacktestIndex,
  BacktestSummary,
  ForecastSnapshot,
  ForecastTeam,
  HistoryEntry,
  HistoryIndex,
  LatestManifest,
  SeasonIndex,
} from "./types";

export type DataResult<T> =
  | { status: "ok"; data: T }
  | { status: "unavailable" };

const unavailable = { status: "unavailable" } as const;
const tolerance = 1e-12;
const maxIndexEntries = 4096;
const maxDocumentBytes = 2 * 1024 * 1024;
const requestDeadlineMs = 5000;
const missing = { status: "not-found" } as const;
const lastSnapshots = new Map<string, ForecastSnapshot>();
type LatestSnapshotResult = DataResult<ForecastSnapshot>
  | { status: "fallback"; data: ForecastSnapshot };
const snapshotKeys = [
  "schema_version", "season_id", "snapshot_id", "generated_at",
  "data_cutoff_at", "model_version",
] as const;

function fields<K extends string>(
  value: unknown,
  keys: readonly K[],
): value is Record<K, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function id(value: unknown): value is string {
  return typeof value === "string" && value.length <= 64
    && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(value) && value.trim() === value;
}

function text(value: unknown): value is string {
  if (typeof value !== "string" || /^[\p{White_Space}\uFEFF]|[\p{White_Space}\uFEFF]$/u.test(value)) return false;
  const characters = Array.from(value);
  return characters.length > 0 && characters.length <= 256
    && characters.every((character) => character.charCodeAt(0) >= 32);
}

function number(value: unknown, low: number, high: number): value is number {
  return typeof value === "number" && Number.isFinite(value)
    && value >= low && value <= high;
}

function integer(value: unknown, low: number, high: number): value is number {
  return number(value, low, high) && Number.isSafeInteger(value);
}

function utc(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$/.test(value)
      || value.trim() !== value || value.startsWith("0000")) return false;
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    && date.toISOString().slice(0, 19) === value.slice(0, 19);
}

function timeKey(value: string): string {
  return value.slice(0, 19) + value.slice(19, -1).slice(1).padEnd(6, "0");
}

function times(value: { generated_at: unknown; data_cutoff_at: unknown }): boolean {
  return utc(value.generated_at) && utc(value.data_cutoff_at)
    && timeKey(value.data_cutoff_at) <= timeKey(value.generated_at);
}

const latestPath = (season: string) => `/data/seasons/${season}/latest.json`;
const snapshotPath = (season: string, snapshot: string) =>
  `/data/seasons/${season}/snapshots/${snapshot}.json`;
const historyPath = (season: string) => `/data/seasons/${season}/history.json`;
const backtestPath = (report: string) => `/data/backtests/${report}.json`;

function seasonIndex(value: unknown): value is SeasonIndex {
  if (!fields(value, [
    "schema_version", "active_season_id", "display_season_id",
    "season_status", "latest_manifest_path",
  ]) || value.schema_version !== 1) return false;
  const { active_season_id: active, display_season_id: display, season_status: status } = value;
  return (display === null || id(display))
    && (status === "active" ? id(active) && active === display
      : (status === "pending" || status === "blocked") && active === null)
    && value.latest_manifest_path === (display === null ? null : latestPath(display));
}

function metadata(value: Record<(typeof snapshotKeys)[number], unknown>): boolean {
  return value.schema_version === 1 && id(value.season_id) && id(value.snapshot_id)
    && text(value.model_version) && times(value);
}

function latestManifest(value: unknown): value is LatestManifest {
  return fields(value, [...snapshotKeys, "snapshot_path", "input_hash"])
    && metadata(value) && id(value.season_id) && id(value.snapshot_id)
    && value.snapshot_path === snapshotPath(value.season_id, value.snapshot_id)
    && typeof value.input_hash === "string" && value.input_hash.length === 64
    && /^[0-9a-f]+$/.test(value.input_hash);
}

function total(values: readonly number[], expected: number): boolean {
  // Compensate for rounding so valid producer totals near the shared
  // tolerance are not rejected by ordinary left-to-right addition.
  let sum = 0;
  let correction = 0;
  for (const value of values) {
    const next = sum + value;
    correction += Math.abs(sum) >= Math.abs(value)
      ? (sum - next) + value : (value - next) + sum;
    sum = next;
  }
  return Math.abs((sum + correction) - expected) <= tolerance;
}

function forecastTeam(value: unknown): value is ForecastTeam {
  if (!fields(value, [
    "team_id", "name", "actual_rank", "played", "actual_points", "expected_points",
    "predicted_rank", "title_probability", "relegation_probability", "position_probabilities",
  ])) return false;
  const row: unknown = value.position_probabilities;
  return id(value.team_id) && text(value.name)
    && integer(value.actual_rank, 1, 20) && integer(value.predicted_rank, 1, 20)
    && integer(value.played, 0, 38)
    && integer(value.actual_points, -Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER)
    && number(value.expected_points, value.actual_points, Number.MAX_SAFE_INTEGER)
    && number(value.title_probability, 0, 1) && number(value.relegation_probability, 0, 1)
    && Array.isArray(row) && row.length === 20
    && row.every((p: unknown): p is number => number(p, 0, 1))
    && total(row, 1) && total([value.title_probability, -row[0]], 0)
    && total([value.relegation_probability, ...row.slice(-3).map((p) => -p)], 0);
}

function forecastSnapshot(value: unknown): value is ForecastSnapshot {
  if (!fields(value, [...snapshotKeys, "simulation_count", "representative_table_method", "teams"])
      || !metadata(value) || !integer(value.simulation_count, 1, Number.MAX_SAFE_INTEGER)
      || !text(value.representative_table_method)) return false;
  const teams: unknown = value.teams;
  if (!Array.isArray(teams) || teams.length !== 20 || !teams.every(forecastTeam)) return false;
  const official = teams.map((team) => team.actual_rank).sort((a, b) => a - b);
  return new Set(teams.map((team) => team.team_id)).size === 20
    && new Set(teams.map((team) => team.predicted_rank)).size === 20
    && official.every((rank, index) => rank === index + 1 || rank === official[index - 1])
    && teams.reduce((games, team) => games + team.played, 0) % 2 === 0
    && total(teams.map((team) => team.title_probability), 1)
    && total(teams.map((team) => team.relegation_probability), 3)
    && official.every((_, index) => total(teams.map((team) => team.position_probabilities[index]), 1));
}

function historyEntry(value: unknown, season: string): value is HistoryEntry {
  return fields(value, ["snapshot_id", "generated_at", "data_cutoff_at", "matchweek_label", "path"])
    && id(value.snapshot_id) && text(value.matchweek_label) && times(value)
    && value.path === snapshotPath(season, value.snapshot_id);
}

function historyIndex(value: unknown): value is HistoryIndex {
  if (!fields(value, ["schema_version", "season_id", "snapshots"])
      || value.schema_version !== 1 || !id(value.season_id)) return false;
  const season = value.season_id;
  const entries: unknown = value.snapshots;
  return Array.isArray(entries) && entries.length <= maxIndexEntries
    && entries.every((entry): entry is HistoryEntry => historyEntry(entry, season))
    && new Set(entries.map((entry) => entry.snapshot_id)).size === entries.length
    && entries.every((entry, i) => i === 0 || timeKey(entries[i - 1].generated_at) <= timeKey(entry.generated_at));
}

function backtestEntry(value: unknown): value is BacktestEntry {
  return fields(value, ["backtest_id", "season_id", "model_version", "path"])
    && id(value.backtest_id) && value.backtest_id !== "index" && id(value.season_id)
    && text(value.model_version) && value.path === backtestPath(value.backtest_id);
}

function backtestIndex(value: unknown): value is BacktestIndex {
  return fields(value, ["schema_version", "reports"]) && value.schema_version === 1
    && Array.isArray(value.reports) && value.reports.length <= maxIndexEntries
    && value.reports.every(backtestEntry)
    && new Set(value.reports.map((entry) => entry.backtest_id)).size === value.reports.length;
}

function backtestSummary(value: unknown): value is BacktestSummary {
  if (!fields(value, [
    "schema_version", "season_id", "model_version", "training_period", "cutoff_policy",
    "sample_size", "baseline", "multiclass_brier_model", "multiclass_brier_baseline", "limitations",
  ])) return false;
  const period: unknown = value.training_period;
  return value.schema_version === 1 && id(value.season_id) && text(value.model_version)
    && text(value.cutoff_policy) && text(value.baseline) && value.sample_size === 380
    && number(value.multiclass_brier_model, 0, 2) && number(value.multiclass_brier_baseline, 0, 2)
    && Array.isArray(value.limitations) && value.limitations.length > 0
    && value.limitations.length <= 16 && value.limitations.every(text)
    && (period === null || (Array.isArray(period) && period.length === 2
      && utc(period[0]) && utc(period[1]) && timeKey(period[0]) <= timeKey(period[1])));
}

async function readJson(response: Response, signal: AbortSignal): Promise<unknown> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Missing static JSON body");
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener("abort", cancel, { once: true });
  let bytes = 0;
  let body = "";
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      bytes += chunk.value.byteLength;
      if (bytes > maxDocumentBytes) throw new Error("Static JSON exceeds size limit");
      body += decoder.decode(chunk.value, { stream: true });
    }
    signal.throwIfAborted();
    return JSON.parse(body + decoder.decode()) as unknown;
  } catch (error) {
    cancel();
    throw error;
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}

async function request<T>(
  path: string,
  validate: (value: unknown) => value is T,
  cache: RequestCache,
): Promise<DataResult<T> | typeof missing> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<typeof unavailable>((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve(unavailable);
    }, requestDeadlineMs);
  });
  async function fetchAndValidate(): Promise<DataResult<T> | typeof missing> {
    try {
      const response = await fetch(path, {
        method: "GET", mode: "same-origin", credentials: "omit", redirect: "error", cache,
        signal: controller.signal,
      });
      if (controller.signal.aborted || !response.ok
          || response.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") {
        void response.body?.cancel().catch(() => {});
        return !controller.signal.aborted && response.status === 404 ? missing : unavailable;
      }
      const value = await readJson(response, controller.signal);
      return validate(value) ? { status: "ok", data: value } : unavailable;
    } catch {
      return unavailable;
    }
  }
  try {
    return await Promise.race([fetchAndValidate(), deadline]);
  } finally {
    clearTimeout(timer);
  }
}

async function load<T>(
  path: string,
  validate: (value: unknown) => value is T,
  cache: RequestCache = "no-cache",
): Promise<DataResult<T>> {
  const result = await request(path, validate, cache);
  return result.status === "not-found" ? unavailable : result;
}

export function loadSeasonIndex(): Promise<DataResult<SeasonIndex>> {
  return load("/data/index.json", seasonIndex);
}

export async function loadLatestManifest(index: SeasonIndex): Promise<DataResult<LatestManifest>> {
  if (!seasonIndex(index) || index.display_season_id === null) return unavailable;
  const season = index.display_season_id;
  return load(latestPath(season), (value): value is LatestManifest =>
    latestManifest(value) && value.season_id === season);
}

function requestLatestSnapshot(manifest: LatestManifest, cache: RequestCache) {
  return request(snapshotPath(manifest.season_id, manifest.snapshot_id), (value): value is ForecastSnapshot =>
    forecastSnapshot(value) && value.season_id === manifest.season_id
      && value.snapshot_id === manifest.snapshot_id && value.model_version === manifest.model_version
      && value.generated_at === manifest.generated_at && value.data_cutoff_at === manifest.data_cutoff_at,
  cache);
}

export async function loadLatestSnapshot(manifest: LatestManifest): Promise<LatestSnapshotResult> {
  if (!latestManifest(manifest)) return unavailable;
  const season = manifest.season_id;
  let result = await requestLatestSnapshot(manifest, "default");
  if (result.status === "not-found") {
    const refreshed = await load(latestPath(season), (value): value is LatestManifest =>
      latestManifest(value) && value.season_id === season);
    if (refreshed.status === "ok") {
      // A cached 404 for the original path must not suppress the retry.
      result = await requestLatestSnapshot(refreshed.data, "reload");
    }
  }
  if (result.status === "ok") {
    const previous = lastSnapshots.get(season);
    if (!previous || timeKey(result.data.generated_at) >= timeKey(previous.generated_at)) {
      lastSnapshots.delete(season);
      lastSnapshots.set(season, structuredClone(result.data));
      if (lastSnapshots.size > 8) {
        const oldest = lastSnapshots.keys().next();
        if (!oldest.done) lastSnapshots.delete(oldest.value);
      }
    }
    return result;
  }
  const cached = lastSnapshots.get(season);
  return cached ? { status: "fallback", data: structuredClone(cached) } : unavailable;
}

export async function loadHistory(index: SeasonIndex): Promise<DataResult<HistoryIndex>> {
  if (!seasonIndex(index) || index.display_season_id === null) return unavailable;
  const season = index.display_season_id;
  return load(historyPath(season), (value): value is HistoryIndex =>
    historyIndex(value) && value.season_id === season);
}

export async function loadHistorySnapshot(
  index: HistoryIndex, snapshotId: string,
): Promise<DataResult<ForecastSnapshot>> {
  if (!historyIndex(index)) return unavailable;
  const entry = index.snapshots.find((snapshot) => snapshot.snapshot_id === snapshotId);
  if (!entry) return unavailable;
  return load(snapshotPath(index.season_id, entry.snapshot_id), (value): value is ForecastSnapshot =>
    forecastSnapshot(value) && value.season_id === index.season_id
      && value.snapshot_id === entry.snapshot_id && value.generated_at === entry.generated_at
      && value.data_cutoff_at === entry.data_cutoff_at, "default");
}

export function loadBacktestIndex(): Promise<DataResult<BacktestIndex>> {
  return load("/data/backtests/index.json", backtestIndex);
}

export async function loadBacktest(
  index: BacktestIndex, backtestId: string,
): Promise<DataResult<BacktestSummary>> {
  if (!backtestIndex(index)) return unavailable;
  const entry = index.reports.find((report) => report.backtest_id === backtestId);
  if (!entry) return unavailable;
  return load(backtestPath(entry.backtest_id), (value): value is BacktestSummary =>
    backtestSummary(value) && value.season_id === entry.season_id
      && value.model_version === entry.model_version, "default");
}
