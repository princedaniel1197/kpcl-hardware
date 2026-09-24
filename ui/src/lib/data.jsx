// The shared data the pages read. It is filled by the DataProvider in App.jsx,
// which holds the dashboard's one poll (§528: a 2-3 s refresh) and the live
// event stream.

import { createContext, useContext } from 'react'

export const DataContext = createContext(null)
export const useData = () => useContext(DataContext)

/** Tags filtered to the selected station. Tags attached to no station (the
 *  collector's own health, load-test tags) appear under "All stations" only. */
export function inStation(station, item) {
  if (!station || station === 'ALL') return true
  return item?.station === station
}
