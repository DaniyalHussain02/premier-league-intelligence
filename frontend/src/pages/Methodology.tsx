import { useEffect, useState } from 'react'
import { loadBacktest, loadBacktestIndex } from '../lib/dataClient'
import { formatCount, formatUtc } from '../lib/format'
import type { BacktestIndex, BacktestSummary } from '../lib/types'
import '../styles/history.css'

type ReportState = { status: 'loading' | 'unavailable' } | { status: 'ready'; report: BacktestSummary }
type IndexState = { status: 'loading' | 'unavailable' } | { status: 'ready'; index: BacktestIndex }
const score = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 6 })

function Evaluation({ index, id }: { index: BacktestIndex; id: string }) {
  const [state, setState] = useState<ReportState>({ status: 'loading' })
  useEffect(() => {
    let active = true
    void loadBacktest(index, id).then((result) => {
      if (active) setState(result.status === 'ok'
        ? { status: 'ready', report: result.data }
        : { status: 'unavailable' })
    }).catch(() => { if (active) setState({ status: 'unavailable' }) })
    return () => { active = false }
  }, [index, id])

  if (state.status !== 'ready') {
    return <p role="status">{state.status === 'loading'
      ? 'Loading retrospective evaluation…' : 'This retrospective report is unavailable.'}</p>
  }
  const { report } = state

  return (
    <>
      <dl className="forecast-metadata">
        <div><dt>Evaluation season</dt><dd>{report.season_id}</dd></div>
        <div>
          <dt>Training period</dt>
          <dd>{report.training_period
            ? <><time dateTime={report.training_period[0]}>{formatUtc(report.training_period[0])}</time>
              {' to '}<time dateTime={report.training_period[1]}>{formatUtc(report.training_period[1])}</time></>
            : 'Unavailable in this report'}</dd>
        </div>
        <div><dt>Sample size</dt><dd>{formatCount(report.sample_size)} matches</dd></div>
        <div><dt>Model version</dt><dd>{report.model_version}</dd></div>
        <div><dt>Historical cutoff policy</dt><dd>{report.cutoff_policy}</dd></div>
        <div><dt>Baseline</dt><dd>{report.baseline}</dd></div>
        <div><dt>Model multiclass Brier score</dt><dd>{score.format(report.multiclass_brier_model)}</dd></div>
        <div><dt>Baseline multiclass Brier score</dt><dd>{score.format(report.multiclass_brier_baseline)}</dd></div>
      </dl>
      <h4>Report limitations</h4>
      <ul>{report.limitations.map((limitation, i) => <li key={i}>{limitation}</li>)}</ul>
    </>
  )
}

export default function Methodology() {
  const [state, setState] = useState<IndexState>({ status: 'loading' })
  const [selection, setSelection] = useState('')
  useEffect(() => {
    let active = true
    void loadBacktestIndex().then((result) => {
      if (active) setState(result.status === 'ok'
        ? { status: 'ready', index: result.data }
        : { status: 'unavailable' })
    }).catch(() => { if (active) setState({ status: 'unavailable' }) })
    return () => { active = false }
  }, [])

  const index = state.status === 'ready' ? state.index : undefined
  const selected = selection || index?.reports[0]?.backtest_id

  return (
    <section aria-labelledby="methodology-heading">
      <h2 id="methodology-heading">Methodology</h2>
      <p>Team strength updates from verified results. An Elo and Poisson model estimates match scores,
        and simulations estimate finishing positions, title chances, and relegation chances. Those probabilities
        describe uncertainty about the final standings.</p>
      <p>The predicted table assigns each club one unique finishing position to maximize the sum of the
        assigned position probabilities. Projected points are each club’s average points across simulations.</p>
      <p>Forecasts depend on source coverage, when verified results become available, and the model’s assumptions. Source delays can leave recent results outside a forecast’s data cutoff.
        A single evaluation cannot establish calibration or predict performance in other seasons.</p>
      <section className="retrospective-report" aria-labelledby="retrospective-heading">
        <h3 id="retrospective-heading">Retrospective evaluation</h3>
        <p>These reports evaluate reconstructed predictions from completed seasons using information
          available at each historical cutoff. Live published forecasts are available in Forecast history.</p>
        <p>The multiclass Brier score averages the summed squared probability errors for home win, draw,
          and away win across the same matches. Scores range from 0 to 2, with lower scores indicating
          better predictions on that sample.</p>
        {state.status === 'loading' && <p role="status">Loading retrospective reports…</p>}
        {(state.status === 'unavailable' || (index && index.reports.length === 0)) &&
          <p role="status">No verified retrospective report is available.</p>}
        {index && selected && (
          <>
            {index.reports.length > 1 && (
              <label className="history-control">
                Retrospective report
                <select value={selected} onChange={(event) => setSelection(event.target.value)}>
                  {index.reports.map((entry) => (
                    <option key={entry.backtest_id} value={entry.backtest_id}>
                      {entry.season_id} · {entry.model_version} · {entry.backtest_id}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <Evaluation key={selected} index={index} id={selected} />
          </>
        )}
      </section>
    </section>
  )
}
