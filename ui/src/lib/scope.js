// The station and period the header selects, as the pages read them.
import { useRouter } from './router'
import { periodOf } from './format'
import { STATION_LABELS } from '../components/Shell'

export function useScope() {
  const { query } = useRouter()
  const station = query.get('station') ?? 'ALL'
  const period = periodOf(query.get('period') ?? undefined)
  return {
    station,
    stationLabel: station === 'ALL' ? 'All stations' : (STATION_LABELS[station] ?? station),
    period: period.id,
    periodLabel: period.label,
    minutes: period.minutes,
  }
}
