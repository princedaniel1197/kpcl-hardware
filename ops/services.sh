#!/usr/bin/env bash
# Start or stop the whole demonstrator in the background:
#
#   ops/services.sh start     database, simulator (bridging the bench rig),
#                             collector, engine, API, UI
#   ops/services.sh stop      the same, stopped cleanly, the database last
#   ops/services.sh status
#
# Logs go to logs/<name>.log and process ids to .run/<name>.pid; neither is
# committed. The rig's address comes from RIG_MODBUS_HOST (default below: the
# DHCP address it had on 24 Sep 2026, so it may need setting); with the rig
# absent the bridge publishes its points BadNoCommunication and everything else
# runs as normal.
#
# "Cleanly" means SIGTERM and a wait: the collector flushes its compressors and
# buffer to disk on SIGTERM, the engine finishes its cycle, and uvicorn closes
# its connections. The collector is stopped after the simulator so nothing is
# acquired half-way through the stop, and TimescaleDB goes last so the
# collector's final forward can land. The SLDC scraper runs under launchd and is
# not touched (make scraper-uninstall stops it) -- and while it is loaded the
# database is left up, because the scraper writes to it and the SLDC page keeps
# no history: time the database is down is time that cannot be recorded.

set -u
cd "$(dirname "$0")/.."
VPY=.venv/bin/python
RIG_MODBUS_HOST=${RIG_MODBUS_HOST:-192.168.1.89}
mkdir -p logs .run

# name|command, in start order.
SERVICES=(
  "sim|$VPY -m sim --endpoint opc.tcp://127.0.0.1:4840/orianode/crpms/ --control-port 8081 --modbus-host $RIG_MODBUS_HOST --modbus-port 502"
  "collector|$VPY -m collector"
  "engine|$VPY -m engine"
  "api|$VPY -m api.main --port 8000"
  "ui|npm --prefix ui run dev"
)
# Stop order: the UI and API first (nothing depends on them), then the source,
# then the collector, then the engine.
STOP_ORDER=(ui api sim collector engine)

running() {
  local pidfile=.run/$1.pid
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

start() {
  make --no-print-directory up || exit 1
  for entry in "${SERVICES[@]}"; do
    local name=${entry%%|*} cmd=${entry#*|}
    if running "$name"; then
      echo "  $name already running (pid $(cat .run/$name.pid))"
      continue
    fi
    echo "--- started $(date -u +%FT%TZ)" >> "logs/$name.log"
    # shellcheck disable=SC2086
    nohup $cmd >> "logs/$name.log" 2>&1 &
    echo $! > ".run/$name.pid"
    echo "  $name started (pid $!, log logs/$name.log)"
    # The collector must find the simulator's endpoint on its first try.
    [ "$name" = sim ] && sleep 4
  done
  echo "UI: http://localhost:5173"
}

stop_one() {
  local name=$1 pidfile=.run/$1.pid
  if ! running "$name"; then
    rm -f "$pidfile"
    return
  fi
  local pid; pid=$(cat "$pidfile")
  # npm starts vite as a child: signal the whole process group's children too.
  pkill -TERM -P "$pid" 2>/dev/null
  kill -TERM "$pid" 2>/dev/null
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "  $name did not stop within 15 s; left running (pid $pid)"
    return 1
  fi
  rm -f "$pidfile"
  echo "  $name stopped"
}

stop() {
  local failed=0
  for name in "${STOP_ORDER[@]}"; do
    stop_one "$name" || failed=1
  done
  if [ $failed -ne 0 ]; then
    echo "not stopping TimescaleDB while a service is still running"
    return 1
  fi
  if launchctl list 2>/dev/null | grep -q com.orianode.crpms.sldc-recorder; then
    echo "  TimescaleDB left running: the SLDC recorder (launchd) writes to it."
    echo "  To stop both: make scraper-uninstall && make down"
  else
    make --no-print-directory down
  fi
}

status() {
  for entry in "${SERVICES[@]}"; do
    local name=${entry%%|*}
    if running "$name"; then echo "  $name running (pid $(cat .run/$name.pid))"
    else echo "  $name stopped"; fi
  done
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status"; exit 2 ;;
esac
