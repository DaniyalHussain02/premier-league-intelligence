export interface StaticDocument {
  schema_version: 1;
}

export interface SeasonIndex extends StaticDocument {
  active_season_id: string | null;
  display_season_id: string | null;
  season_status: "active" | "pending" | "blocked";
  latest_manifest_path: string | null;
}

interface SnapshotMetadata extends StaticDocument {
  season_id: string;
  snapshot_id: string;
  generated_at: string;
  data_cutoff_at: string;
  model_version: string;
}

export interface LatestManifest extends SnapshotMetadata {
  snapshot_path: string;
  input_hash: string;
}

export interface ForecastTeam {
  team_id: string;
  name: string;
  actual_rank: number;
  played: number;
  /** Official adjusted points, on the same basis as expected_points. */
  actual_points: number;
  expected_points: number;
  predicted_rank: number;
  title_probability: number;
  relegation_probability: number;
  position_probabilities: number[];
}

export interface ForecastSnapshot extends SnapshotMetadata {
  simulation_count: number;
  representative_table_method: string;
  teams: ForecastTeam[];
}

export interface HistoryEntry {
  snapshot_id: string;
  generated_at: string;
  data_cutoff_at: string;
  matchweek_label: string;
  path: string;
}

export interface HistoryIndex extends StaticDocument {
  season_id: string;
  snapshots: HistoryEntry[];
}

export interface BacktestEntry {
  backtest_id: string;
  season_id: string;
  model_version: string;
  path: string;
}

export interface BacktestIndex extends StaticDocument {
  reports: BacktestEntry[];
}

/** Retrospective evaluation from the separate backtests index. */
export interface BacktestSummary extends StaticDocument {
  season_id: string;
  model_version: string;
  training_period: [string, string] | null;
  cutoff_policy: string;
  sample_size: number;
  baseline: string;
  multiclass_brier_model: number;
  multiclass_brier_baseline: number;
  limitations: string[];
}
