#!/usr/bin/env bash
# Control HermitWatch on the Pi from this computer (over Tailscale SSH).
#
#   ./hermitwatch.sh start     start it (does nothing if already running)
#   ./hermitwatch.sh stop      stop it
#   ./hermitwatch.sh restart   stop, then start
#   ./hermitwatch.sh deploy    copy cam_stream.py, recorder.py, ui.html to the Pi, then restart
#   ./hermitwatch.sh status    running or not, plus the stream URL
#   ./hermitwatch.sh logs      follow the server log (Ctrl-C to quit)
#
# Target Pi: $HERMITWATCH_HOST, else the first line of .hermitwatch-host next to
# this script (git-ignored, so your host name stays out of the repo), else pi@raspberrypi.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_FILE="$DIR/.hermitwatch-host"
if [ -n "${HERMITWATCH_HOST:-}" ]; then
  HOST="$HERMITWATCH_HOST"
elif [ -s "$HOST_FILE" ]; then
  HOST="$(head -n 1 "$HOST_FILE" | tr -d '[:space:]')"
else
  HOST="pi@raspberrypi"
fi
NAME="${HOST#*@}"
# Pattern anchored on "python3" so pkill/pgrep never match this script's own ssh command.
PATTERN='^python3 .*cam_stream'

remote() { ssh -o ConnectTimeout=10 "$HOST" "$@"; }

running() { remote "pgrep -f '$PATTERN' >/dev/null"; }

start() {
  if running; then
    echo "HermitWatch is already running on $NAME."
  else
    remote "nohup python3 ~/cam_stream.py > ~/cam_stream.log 2>&1 < /dev/null &"
    for _ in $(seq 1 15); do
      sleep 1
      if remote "grep -q 'Streaming on' ~/cam_stream.log"; then
        echo "HermitWatch started."
        status
        return
      fi
    done
    echo "HermitWatch did not start. Last log lines:" >&2
    remote "tail -20 ~/cam_stream.log" >&2
    exit 1
  fi
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
  else
    echo "Not running on $NAME."
  fi
}

deploy() {
  scp -q "$DIR/cam_stream.py" "$DIR/recorder.py" "$DIR/ui.html" "$HOST:~/"
  echo "Copied cam_stream.py, recorder.py and ui.html to $NAME."
  stop
  start
}

case "${1:-start}" in start|stop|restart|deploy|status|logs)
  remote true 2>/dev/null || { echo "Cannot reach $HOST over SSH (is it on and on Tailscale?)." >&2; exit 1; } ;;
esac

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  deploy) deploy ;;
  status) status ;;
  logs) remote "tail -n 50 -f ~/cam_stream.log" ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
