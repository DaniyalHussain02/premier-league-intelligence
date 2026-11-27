// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  loadBacktest, loadBacktestIndex, loadHistory, loadHistorySnapshot,
  loadLatestManifest, loadLatestSnapshot, loadSeasonIndex,
} from "./dataClient";
import type { ForecastSnapshot, LatestManifest, SeasonIndex } from "./types";

function manifest(season = "2026-2027", snapshot = "forecast-01"): LatestManifest {
  return {
    schema_version: 1, season_id: season, snapshot_id: snapshot,
    generated_at: "2026-11-24T12:00:00.000001Z", data_cutoff_at: "2026-11-24T12:00:00Z",
    model_version: "model-v1", snapshot_path: `/data/seasons/${season}/snapshots/${snapshot}.json`,
    input_hash: "a".repeat(64),
  };
}

function snapshot(ref: LatestManifest): ForecastSnapshot {
  const { input_hash, snapshot_path, ...metadata } = ref;
  void input_hash;
  void snapshot_path;
  return {
    ...metadata, simulation_count: 10000, representative_table_method: "maximum-weight",
    teams: Array.from({ length: 20 }, (_, i) => ({
      team_id: `club-${i}`, name: `Club ${i}`, actual_rank: 1, predicted_rank: i + 1,
      played: i < 2 ? 9 : 10, actual_points: -1, expected_points: 10.25,
      title_probability: Number(i === 0), relegation_probability: Number(i >= 17),
      position_probabilities: Array.from({ length: 20 }, (_, j) => Number(i === j)),
    })),
  };
}

const json = (data: unknown) => new Response(JSON.stringify(data), {
  headers: { "content-type": "application/json; charset=utf-8" },
});
const missing = () => new Response(null, { status: 404 });

function mockFetch() {
  const fetch = vi.fn<typeof globalThis.fetch>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("latest forecast recovery", () => {
  it("refreshes the validated manifest and retries once after a snapshot 404", async () => {
    const fetch = mockFetch();
    const old = manifest("season-retry");
    const next = manifest(old.season_id, "new-snapshot");
    fetch.mockResolvedValueOnce(missing()).mockResolvedValueOnce(json(next))
      .mockResolvedValueOnce(json(snapshot(next)));
    expect(await loadLatestSnapshot(old)).toEqual({ status: "ok", data: snapshot(next) });
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      old.snapshot_path, `/data/seasons/${old.season_id}/latest.json`, next.snapshot_path,
    ]);
    expect(fetch.mock.calls[1][1]?.cache).toBe("no-cache");
  });

  it("returns an explicit same-season fallback with the original timestamps", async () => {
    const fetch = mockFetch();
    const old = manifest("season-fallback");
    const saved = snapshot(old);
    const next = { ...manifest(old.season_id, "new"), generated_at: "2026-11-25T12:00:00Z" };
    fetch.mockResolvedValueOnce(json(saved));
    expect((await loadLatestSnapshot(old)).status).toBe("ok");
    fetch.mockResolvedValueOnce(missing()).mockResolvedValueOnce(json(next)).mockResolvedValueOnce(missing());
    expect(await loadLatestSnapshot(next)).toEqual({ status: "fallback", data: saved });
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it("stops on the second 404 with no same-season fallback", async () => {
    const fetch = mockFetch();
    const other = manifest("season-other");
    fetch.mockResolvedValueOnce(json(snapshot(other)));
    await loadLatestSnapshot(other);
    const cold = manifest("season-cold");
    fetch.mockResolvedValueOnce(missing()).mockResolvedValueOnce(json(cold)).mockResolvedValueOnce(missing());
    expect(await loadLatestSnapshot(cold)).toEqual({ status: "unavailable" });
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it("rejects the refreshed manifest if it points at a different season", async () => {
    const fetch = mockFetch();
    fetch.mockResolvedValueOnce(missing()).mockResolvedValueOnce(json(manifest("other-season")));
    expect(await loadLatestSnapshot(manifest("requested-season"))).toEqual({ status: "unavailable" });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe("static contract validation", () => {
  it("uses credential-free static GET with separate retrospective routes", async () => {
    const fetch = mockFetch();
    const report = {
      backtest_id: "approved-01", season_id: "2025-2026",
      model_version: "model-v1", path: "/data/backtests/approved-01.json",
    };
    fetch.mockResolvedValueOnce(json({ schema_version: 1, reports: [report] }))
      .mockResolvedValueOnce(json({
        schema_version: 1, season_id: report.season_id, model_version: report.model_version,
        training_period: null, cutoff_policy: "first timer", sample_size: 380,
        baseline: "empirical", multiclass_brier_model: 0.4,
        multiclass_brier_baseline: 0.5, limitations: ["Retrospective evaluation."],
      }));
    const index = await loadBacktestIndex();
    expect(index.status).toBe("ok");
    if (index.status !== "ok") throw new Error("Missing index");
    expect((await loadBacktest(index.data, report.backtest_id)).status).toBe("ok");
    expect(fetch.mock.calls.map(([path]) => path)).toEqual(["/data/backtests/index.json", report.path]);
    for (const [, options] of fetch.mock.calls) {
      expect(options).toMatchObject({ method: "GET", mode: "same-origin", credentials: "omit", redirect: "error" });
    }
  });

  it("handles no display season without a manifest request", async () => {
    const fetch = mockFetch();
    const off: SeasonIndex = {
      schema_version: 1, active_season_id: null, display_season_id: null,
      season_status: "pending", latest_manifest_path: null,
    };
    fetch.mockResolvedValueOnce(json(off));
    expect(await loadSeasonIndex()).toEqual({ status: "ok", data: off });
    expect((await loadLatestManifest(off)).status).toBe("unavailable");
    expect((await loadHistory(off)).status).toBe("unavailable");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("rejects encoded or unlisted paths before fetch", async () => {
    const fetch = mockFetch();
    const ref = manifest();
    expect((await loadLatestSnapshot({ ...ref, snapshot_id: "%2e%2e" })).status).toBe("unavailable");
    expect((await loadLatestSnapshot({ ...ref, snapshot_path: "//other.test/a" })).status).toBe("unavailable");
    expect((await loadHistorySnapshot({ schema_version: 1, season_id: ref.season_id, snapshots: [] }, "../a")).status).toBe("unavailable");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("accepts shared ranks, adjusted points, near-normalized rows, and precise UTC timestamps", async () => {
    const fetch = mockFetch();
    const ref = manifest("season-valid");
    const valid = snapshot(ref);
    valid.teams[1].position_probabilities[1] -= 5e-13;
    valid.teams[1].name = "Club\u00a01";
    fetch.mockResolvedValueOnce(json(valid));
    expect((await loadLatestSnapshot(ref)).status).toBe("ok");
  });

  it.each([
    ["odd team games", (s: ForecastSnapshot) => { s.teams[0].played += 1; }],
    ["invalid counts", (s: ForecastSnapshot) => { s.teams[0].played = 9.5; }],
    ["row tolerance", (s: ForecastSnapshot) => { s.teams[1].position_probabilities[1] -= 2e-12; }],
    ["invalid column", (s: ForecastSnapshot) => { s.teams[1].position_probabilities = s.teams[2].position_probabilities; }],
    ["Unicode whitespace", (s: ForecastSnapshot) => { s.teams[0].name = "\ufeffClub"; }],
    ["timestamp mismatch", (s: ForecastSnapshot) => { s.generated_at = "2026-11-24T12:00:00.000002Z"; }],
    ["model mismatch", (s: ForecastSnapshot) => { s.model_version = "other"; }],
  ] as const)("rejects %s", async (_, damage) => {
    const fetch = mockFetch();
    const ref = manifest("season-invalid");
    const invalid = snapshot(ref);
    damage(invalid);
    fetch.mockResolvedValueOnce(json(invalid));
    expect((await loadLatestSnapshot(ref)).status).toBe("unavailable");
  });

  it("rejects malformed and oversized JSON", async () => {
    const fetch = mockFetch();
    for (const body of ["{broken", " ".repeat(2 * 1024 * 1024 + 1)]) {
      fetch.mockResolvedValueOnce(new Response(body, { headers: { "content-type": "application/json" } }));
      expect((await loadSeasonIndex()).status).toBe("unavailable");
    }
  });

  it.each(["fetch", "body"])("applies a deadline to a stalled %s", async (stage) => {
    vi.useFakeTimers();
    const fetch = mockFetch();
    if (stage === "fetch") {
      fetch.mockImplementation(() => new Promise<Response>(() => {}));
    } else {
      fetch.mockResolvedValueOnce(new Response(new ReadableStream(), {
        headers: { "content-type": "application/json" },
      }));
    }
    const request = loadSeasonIndex();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(await request).toEqual({ status: "unavailable" });
  });
});

it("accepts numeric integer values regardless of the JSON token spelling", async () => {
  const fetch = mockFetch();
  const ref = manifest("season-numeric");
  const body = JSON.stringify(snapshot(ref)).replace('"played":9', '"played":9.0');
  fetch.mockResolvedValueOnce(new Response(body, { headers: { "content-type": "application/json" } }));
  expect((await loadLatestSnapshot(ref)).status).toBe("ok");
});

it("accepts the Python producer's near-tolerance probability matrix", async () => {
  const fetch = mockFetch();
  const ref = manifest("season-precision");
  const valid = snapshot(ref);
  // The same vector is validated with Python fsum in test_json_contract.py.
  const row = Array<number>(19).fill(0.05).concat(0.05000000000099969);
  for (const [i, team] of valid.teams.entries()) {
    const values = i < 10 ? row : row.map((value) => 0.1 - value);
    team.position_probabilities = values;
    team.title_probability = values[0];
    team.relegation_probability = values[17] + values[18] + values[19];
  }
  fetch.mockResolvedValueOnce(json(valid));
  expect((await loadLatestSnapshot(ref)).status).toBe("ok");
});

describe("immutable forecast history", () => {
  const ref = manifest("history-season");
  const entry = {
    snapshot_id: ref.snapshot_id, path: ref.snapshot_path, generated_at: ref.generated_at,
    data_cutoff_at: ref.data_cutoff_at, matchweek_label: "Matchweek 1",
  };
  const index = { schema_version: 1 as const, season_id: ref.season_id, snapshots: [entry] };

  it("loads the indexed snapshot by canonical path and preserves stable club IDs", async () => {
    const fetch = mockFetch();
    const data = snapshot(ref);
    fetch.mockResolvedValueOnce(json(data));
    expect(await loadHistorySnapshot(index, entry.snapshot_id)).toEqual({ status: "ok", data });
    expect(fetch).toHaveBeenCalledWith(ref.snapshot_path, expect.objectContaining({
      method: "GET", mode: "same-origin", credentials: "omit", cache: "default",
    }));
  });

  it.each([
    { season_id: "other-season" },
    { snapshot_id: "other-snapshot" },
    { generated_at: "2026-11-24T12:00:00.000002Z" },
    { data_cutoff_at: "2026-11-24T11:00:00Z" },
    { teams: [] },
  ])("rejects malformed or mismatched snapshot fields %j", async (patch) => {
    const fetch = mockFetch();
    fetch.mockResolvedValueOnce(json({ ...snapshot(ref), ...patch }));
    expect(await loadHistorySnapshot(index, entry.snapshot_id)).toEqual({ status: "unavailable" });
  });

  it("rejects an external index path or a snapshot ID absent from the validated index", async () => {
    const fetch = mockFetch();
    const unsafe = { ...index, snapshots: [{ ...entry, path: "https://example.test/forecast.json" }] };
    expect(await loadHistorySnapshot(unsafe, entry.snapshot_id)).toEqual({ status: "unavailable" });
    expect(await loadHistorySnapshot(index, "unlisted-snapshot")).toEqual({ status: "unavailable" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("reports a missing indexed snapshot as unavailable", async () => {
    const fetch = mockFetch();
    fetch.mockResolvedValueOnce(missing());
    expect(await loadHistorySnapshot(index, entry.snapshot_id)).toEqual({ status: "unavailable" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
