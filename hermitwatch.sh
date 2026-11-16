#!/usr/bin/env bash
# Control HermitWatch. Works in two places:
#   - on the Pi, inside the project folder: runs everything locally
#   - on another computer: runs the same commands on the Pi over SSH
#
#   ./hermitwatch.sh start     start it (does nothing if already running)
#   ./hermitwatch.sh stop      stop it
#   ./hermitwatch.sh restart   stop, then start
#   ./hermitwatch.sh update    git pull the project on the Pi, then restart
#   ./hermitwatch.sh status    running or not, plus the stream URL
#   ./hermitwatch.sh logs      follow the server log (Ctrl-C to quit)
#
# Remote target: $HERMITWATCH_HOST, else the first line of .hermitwatch-host next to
# this script (git-ignored, so your host name stays out of the repo), else pi@raspberrypi.
# Project folder on the Pi: $HERMITWATCH_DIR (default ~/hermit-watch).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_FILE="$DIR/.hermitwatch-host"
LOG='$HOME/cam_stream.log'
# Pattern anchored on "python3" so pkill/pgrep never match this script's own commands.
PATTERN='^python3 .*cam_stream'

if [ -z "${HERMITWATCH_HOST:-}" ] && [ ! -s "$HOST_FILE" ] && [ "$(uname)" = Linux ] && [ -f "$DIR/cam_stream.py" ]; then
  # Running on the Pi itself.
  LOCAL=1
  APP="$DIR"
  NAME="$(hostname)"
  remote() { bash -c "$*"; }
else
  LOCAL=0
  if [ -n "${HERMITWATCH_HOST:-}" ]; then
    HOST="$HERMITWATCH_HOST"
  elif [ -s "$HOST_FILE" ]; then
    HOST="$(head -n 1 "$HOST_FILE" | tr -d '[:space:]')"
  else
    HOST="pi@raspberrypi"
  fi
  APP="${HERMITWATCH_DIR:-\$HOME/hermit-watch}"
  NAME="${HOST#*@}"
  remote() { ssh -o ConnectTimeout=10 "$HOST" "$@"; }
fi

running() { remote "pgrep -f '$PATTERN' >/dev/null"; }

start() {
  if running; then
    echo "HermitWatch is already running on $NAME."
    return
  fi
  remote "test -f $APP/cam_stream.py" || { echo "No project at $APP on $NAME (clone the repo there first)." >&2; exit 1; }
  remote "cd $APP && nohup python3 \"\$PWD/cam_stream.py\" > $LOG 2>&1 < /dev/null &"
  for _ in $(seq 1 15); do
    sleep 1
    if remote "grep -q 'Streaming on' $LOG"; then
      echo "HermitWatch started from ${APP/\$HOME/~}."
      status
      return
    fi
  done
  echo "HermitWatch did not start. Last log lines:" >&2
  remote "tail -20 $LOG" >&2
  exit 1
}

stop() {
  if running; then
    remote "pkill -f '$PATTERN'"
    for _ in $(seq 1 10); do
      sleep 1
      running || { echo "HermitWatch stopped."; return; }
    done
    echo "HermitWatch did not stop in 10 s; forcing." >&2
    remote "pkill -9 -f '$PATTERN'" || true
  else
    echo "HermitWatch is not running."
  fi
}

status() {
  if running; then
    echo "Running on $NAME: http://$NAME:8000/  (recordings: http://$NAME:8000/#recordings)"
    remote "pgrep -fa '$PATTERN'" | sed 's/^/  process: /'
  else
    echo "Not running on $NAME."
  fi
}

update() {
  remote "cd $APP && git pull --ff-only"
  stop
  start
}

if [ "$LOCAL" = 0 ]; then
  case "${1:-start}" in start|stop|restart|update|deploy|status|logs)
    remote true 2>/dev/null || { echo "Cannot reach $HOST over SSH (is it on and on Tailscale?)." >&2; exit 1; } ;;
  esac
fi

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  update|deploy) update ;;
  status) status ;;
  logs) remote "tail -n 50 -f $LOG" ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
