import { formatCount, formatProbability, formatProjectedPoints } from '../lib/format'
import type { ForecastTeam } from '../lib/types'

export function ForecastTable({ teams }: { teams: ForecastTeam[] }) {
  const ordered = [...teams].sort((left, right) => left.predicted_rank - right.predicted_rank)
  return (
    <>
      <h2 id="forecast-table-heading">Representative predicted table</h2>
      <p id="forecast-explanation">
        Each club gets one unique finishing position. The positions are chosen together to
        maximize the sum of their probabilities. Clubs can compete for the same spot, so an
        assigned rank can differ from a club’s most likely individual finish. Projected points
        are averages across simulations and are shown separately. Both points columns include
        any official adjustments. Title and relegation probabilities are estimates.
      </p>
      <p id="forecast-scroll-hint" className="scroll-hint">
        Scroll the table sideways to see all columns. You can also focus it and use the arrow keys.
      </p>
      <div
        className="table-scroll"
        role="region"
        tabIndex={0}
        aria-labelledby="forecast-table-heading"
        aria-describedby="forecast-scroll-hint"
      >
        <table aria-describedby="forecast-explanation">
          <caption>Current standings and forecast for all 20 clubs</caption>
          <thead>
            <tr>
              <th scope="col">Predicted rank</th>
              <th scope="col">Club</th>
              <th scope="col">Actual rank</th>
              <th scope="col">Actual points</th>
              <th scope="col">Games played</th>
              <th scope="col">Projected points</th>
              <th scope="col">Title probability</th>
              <th scope="col">Relegation probability</th>
            </tr>
          </thead>
          <tbody>
            {ordered.map((team) => (
              <tr key={team.team_id}>
                <td>{team.predicted_rank}</td>
                <th scope="row">{team.name}</th>
                <td>{team.actual_rank}</td>
                <td>{formatCount(team.actual_points)}</td>
                <td>{team.played}</td>
                <td>{formatProjectedPoints(team.expected_points)}</td>
                <td>{formatProbability(team.title_probability)}</td>
                <td>{formatProbability(team.relegation_probability)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
