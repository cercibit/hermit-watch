#!/usr/bin/env python3
"""HermitWatch: live stream, camera controls and motion-triggered recording for the Pi camera.

Open http://<tailscale-name>:8000/ in a browser.

Security:
  - Binds only to the Tailscale IP (override with BIND=...).
  - Requires HTTP Basic Auth; credentials ("user:password") live in
    ~/.cam_stream_auth (override with AUTH_FILE=...).
  - POSTs must be application/json, so other websites can't change settings.

Camera settings persist in ~/.cam_stream_settings.json (SETTINGS_FILE=...).
Env vars: PORT (default 8000), WIDTH/HEIGHT (default 1296x972).

Endpoints:
  /               viewer + control panel
  /stream.mjpg    raw MJPEG stream (VLC/ffplay)
  /snapshot.jpg   single JPEG frame
  /api/settings   GET schema+values, POST partial update ({"reset": true} resets)
  /api/status     GET live exposure/gain/white-balance metadata + motion state
  /recordings     browse motion clips and time-lapses
  /api/recordings GET library; /api/recordings/delete, /api/timelapse/build (POST)
  /media/<path>   recorded files (supports Range for video seeking)

Recordings live in ~/hermitwatch (REC_DIR=...); see recorder.py.
"""
import base64
import hmac
import io
import json
import logging
import math
import os
import socketserver
import subprocess
import threading
from http import server
from threading import Condition

from libcamera import Transform
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, MJPEGEncoder
from picamera2.outputs import CircularOutput2, FileOutput

import recorder as rec

PORT = int(os.environ.get("PORT", 8000))
WIDTH = int(os.environ.get("WIDTH", 1296))
HEIGHT = int(os.environ.get("HEIGHT", 972))
SETTINGS_FILE = os.path.expanduser(os.environ.get("SETTINGS_FILE", "~/.cam_stream_settings.json"))


def tailscale_ip():
    """Return this machine's Tailscale IPv4, so the stream is never exposed on LAN/Wi-Fi."""
    ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True).stdout.strip()
    if not ip:
        raise SystemExit("Tailscale IP not found; refusing to bind to other interfaces.")
    return ip.splitlines()[0]


BIND = os.environ.get("BIND") or tailscale_ip()


def load_auth():
    """Read 'user:password' from AUTH_FILE (default ~/.cam_stream_auth, chmod 600)."""
    path = os.path.expanduser(os.environ.get("AUTH_FILE", "~/.cam_stream_auth"))
    try:
        with open(path) as f:
            creds = f.read().strip()
    except FileNotFoundError:
        raise SystemExit(f"Auth file {path} missing; refusing to start without a password.")
    if ":" not in creds:
        raise SystemExit(f"Auth file {path} must contain user:password")
    return "Basic " + base64.b64encode(creds.encode()).decode()


EXPECTED_AUTH = load_auth()

# ---------------------------------------------------------------------------
# Settings schema (drives validation, defaults and the web UI)
# ---------------------------------------------------------------------------


def rng(id, label, group, lo, hi, step, default, **kw):
    return dict(id=id, label=label, group=group, type="range", min=lo, max=hi, step=step, default=default, **kw)


def sel(id, label, group, options, default, **kw):
    return dict(id=id, label=label, group=group, type="select", options=options, default=default, **kw)


def flag(id, label, group, default, **kw):
    return dict(id=id, label=label, group=group, type="bool", default=default, **kw)


SCHEMA = [
    rng("brightness", "Brightness", "Image", -1, 1, 0.01, 0.0),
    rng("contrast", "Contrast", "Image", 0, 4, 0.05, 1.0),
    rng("saturation", "Saturation", "Image", 0, 4, 0.05, 1.0),
    rng("sharpness", "Sharpness", "Image", 0, 16, 0.1, 1.0),
    sel("denoise", "Noise reduction", "Image", ["Off", "Fast", "High quality", "Minimal"], 1),

    flag("ae_auto", "Auto exposure", "Exposure", True),
    rng("ev", "Exposure compensation (EV)", "Exposure", -8, 8, 0.1, 0.0, when="ae_auto"),
    sel("exposure_mode", "Exposure mode", "Exposure", ["Normal", "Short", "Long"], 0, when="ae_auto"),
    sel("metering", "Metering", "Exposure", ["Centre-weighted", "Spot", "Matrix"], 0, when="ae_auto"),
    sel("constraint", "Constraint", "Exposure", ["Normal", "Protect highlights", "Lift shadows"], 0, when="ae_auto"),
    rng("exposure_us", "Shutter", "Exposure", 100, 1_000_000, 1, 20000, when="!ae_auto", scale="log", unit="us"),
    rng("gain", "Analogue gain", "Exposure", 1, 16, 0.1, 1.0, when="!ae_auto"),
    sel("flicker", "Anti-flicker", "Exposure", ["Off", "50 Hz", "60 Hz"], 0),
    rng("fps", "Max frame rate", "Exposure", 1, 30, 1, 30, unit="fps"),

    flag("awb_auto", "Auto white balance", "Colour", True),
    sel("awb_mode", "White balance preset", "Colour",
        ["Auto", "Incandescent", "Tungsten", "Fluorescent", "Indoor", "Daylight", "Cloudy"], 0, when="awb_auto"),
    rng("red_gain", "Red gain", "Colour", 0.5, 4, 0.01, 1.5, when="!awb_auto"),
    rng("blue_gain", "Blue gain", "Colour", 0.5, 4, 0.01, 1.5, when="!awb_auto"),

    rng("zoom", "Digital zoom", "Framing", 1, 4, 0.05, 1.0, unit="x"),
    rng("pan_x", "Pan left/right", "Framing", 0, 1, 0.01, 0.5),
    rng("pan_y", "Pan up/down", "Framing", 0, 1, 0.01, 0.5),
    flag("hflip", "Flip horizontal", "Framing", False),
    flag("vflip", "Flip vertical", "Framing", False),

    flag("rec_clips", "Record video clips on motion", "Recording", True),
    rng("post_roll", "Keep recording after motion stops", "Recording", 2, 60, 1, 10, unit="s", when="rec_clips"),
    rng("max_clip", "Split clips longer than", "Recording", 30, 900, 10, 300, unit="s", when="rec_clips"),
    flag("tl_enabled", "Time-lapse photos", "Recording", True),
    rng("tl_interval", "Photo every", "Recording", 2, 600, 1, 10, unit="s", scale="log", when="tl_enabled"),
    flag("tl_motion_only", "Only while crabs are moving", "Recording", True, when="tl_enabled"),
    rng("storage_gb", "Storage limit (oldest deleted first)", "Recording", 1, 100, 1, 20, unit="GB"),

    rng("motion_area", "Minimum moving area", "Motion detection", 0.05, 10, 0.05, 0.3, unit="%"),
    rng("motion_threshold", "Pixel change threshold", "Motion detection", 3, 60, 1, 12),
    rng("motion_gap", "Compare frames this far apart", "Motion detection", 0.5, 5, 0.5, 1, unit="s"),
]
FIELDS = {f["id"]: f for f in SCHEMA}
DEFAULTS = {f["id"]: f["default"] for f in SCHEMA}


def validate(update):
    """Keep only known keys, coerced and clamped to their schema."""
    out = {}
    for key, value in update.items():
        f = FIELDS.get(key)
        if f is None:
            continue
        try:
            if f["type"] == "bool":
                out[key] = bool(value)
            elif f["type"] == "select":
                v = int(value)
                if 0 <= v < len(f["options"]):
                    out[key] = v
            else:
                v = float(value)
                if math.isfinite(v):
                    out[key] = min(max(v, f["min"]), f["max"])
        except (TypeError, ValueError):
            pass
    return out


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            s.update(validate(json.load(f)))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return s


settings = load_settings()
settings_lock = threading.Lock()
save_timer = None


def save_settings_soon():
    """Debounced save so slider drags don't hammer the SD card."""
    global save_timer

    def save():
        with settings_lock:
            data = json.dumps(settings, indent=1)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, SETTINGS_FILE)

    if save_timer:
        save_timer.cancel()
    save_timer = threading.Timer(2.0, save)
    save_timer.daemon = True
    save_timer.start()


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


picam2 = Picamera2()
output = StreamingOutput()
camera_lock = threading.Lock()
current_flip = None
pipeline = {"circular": None, "mjpeg": None, "h264": None, "fps": None}
PREROLL_MS = 5000  # each clip starts this long before motion was detected


def build_controls(s):
    """Translate our settings into libcamera controls the sensor supports."""
    available = picam2.camera_controls
    manual_modes = "ExposureTimeMode" in available  # newer libcamera
    c = {
        "Brightness": s["brightness"],
        "Contrast": s["contrast"],
        "Saturation": s["saturation"],
        "Sharpness": s["sharpness"],
        "NoiseReductionMode": s["denoise"],
    }

    frame_us = int(1_000_000 / s["fps"])
    if s["ae_auto"]:
        if manual_modes:
            c.update(ExposureTimeMode=0, AnalogueGainMode=0)
        else:
            c["AeEnable"] = True
        c.update(ExposureValue=s["ev"], AeExposureMode=s["exposure_mode"],
                 AeMeteringMode=s["metering"], AeConstraintMode=s["constraint"])
    else:
        if manual_modes:
            c.update(ExposureTimeMode=1, AnalogueGainMode=1)
        else:
            c["AeEnable"] = False
        exposure = int(s["exposure_us"])
        c.update(ExposureTime=exposure, AnalogueGain=s["gain"])
        frame_us = max(frame_us, exposure)  # long shutter needs a long frame
    c["FrameDurationLimits"] = (frame_us, frame_us)

    if s["flicker"] == 0:
        c["AeFlickerMode"] = 0
    else:
        c.update(AeFlickerMode=1, AeFlickerPeriod=10000 if s["flicker"] == 1 else 8333)

    if s["awb_auto"]:
        c.update(AwbEnable=True, AwbMode=s["awb_mode"])
    else:
        c.update(AwbEnable=False, ColourGains=(s["red_gain"], s["blue_gain"]))

    x0, y0, w, h = picam2.camera_properties["ScalerCropMaximum"]
    cw, ch = int(w / s["zoom"]), int(h / s["zoom"])
    c["ScalerCrop"] = (x0 + int((w - cw) * s["pan_x"]), y0 + int((h - ch) * s["pan_y"]), cw, ch)

    return {k: v for k, v in c.items() if k in available}


def stop_h264():
    # Stopping the encoder flushes the circular buffer into any open clip file.
    if pipeline["h264"]:
        picam2.stop_encoder([pipeline["h264"]])
        pipeline["h264"] = None
        if recorder is not None:
            recorder.close_clip(flushed=True)


def start_h264(fps):
    """H.264 into a rolling pre-roll buffer. Clips can only start on a keyframe, so keep
    keyframes ~1 s apart at any frame rate (a long manual shutter can mean 1 fps)."""
    h264 = H264Encoder(bitrate=3_000_000, repeat=True, iperiod=max(1, round(fps)))
    h264.framerate = fps
    circular = CircularOutput2(buffer_duration_ms=PREROLL_MS)
    picam2.start_encoder(h264, circular, name="main")
    pipeline.update(h264=h264, circular=circular, fps=fps)


def stop_pipeline():
    stop_h264()
    if pipeline["mjpeg"]:
        picam2.stop_encoder([pipeline["mjpeg"]])
        pipeline["mjpeg"] = None
    picam2.stop()


def start_pipeline(flip, fps):
    """main -> MJPEG (live view) and H.264 (pre-roll buffer for clips); lores -> motion."""
    config = picam2.create_video_configuration(
        main={"size": (WIDTH, HEIGHT)},
        lores={"size": rec.LORES, "format": "YUV420"},
        transform=Transform(hflip=int(flip[0]), vflip=int(flip[1])),
    )
    picam2.configure(config)
    mjpeg = MJPEGEncoder()
    picam2.start_encoder(mjpeg, FileOutput(output), name="main")
    pipeline["mjpeg"] = mjpeg
    start_h264(fps)
    picam2.start()


def apply_settings(s):
    """Push settings to the camera; reconfigure only when flip changes."""
    global current_flip
    controls = build_controls(s)
    fps = round(1_000_000 / controls["FrameDurationLimits"][0], 2)
    with camera_lock:
        flip = (bool(s["hflip"]), bool(s["vflip"]))
        if flip != current_flip:
            if current_flip is not None:
                stop_pipeline()
            start_pipeline(flip, fps)
            current_flip = flip
        elif max(fps, pipeline["fps"]) / min(fps, pipeline["fps"]) > 1.5:
            stop_h264()  # frame rate changed a lot: re-pick the keyframe interval
            start_h264(fps)
        picam2.set_controls(controls)


def camera_status():
    try:
        md = picam2.capture_metadata()
    except Exception as e:  # e.g. mid-reconfigure
        return {"error": str(e)}
    keys = ["ExposureTime", "AnalogueGain", "DigitalGain", "ColourGains",
            "ColourTemperature", "Lux", "FrameDuration", "SensorTemperature"]
    out = {k: md[k] for k in keys if k in md}
    if recorder is not None:
        out["motion"] = recorder.status()
    return out


def get_settings():
    with settings_lock:
        return dict(settings)


recorder = None  # created in __main__ once the camera is running


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HermitWatch</title>
<style>
  :root { color-scheme: dark; --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e6e8ee; --dim:#8b93a7; --acc:#e0894a; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 -apple-system,system-ui,sans-serif;
         display:flex; height:100vh; overflow:hidden; }
  #view { flex:1; display:flex; flex-direction:column; min-width:0; }
  #video { flex:1; display:flex; align-items:center; justify-content:center; min-height:0; background:#000; }
  #video img { max-width:100%; max-height:100%; }
  .rec { color:#ff5a5a; font-weight:600; } .mov { color:var(--acc); font-weight:600; }
  #status { padding:8px 14px; font:12px ui-monospace,Menlo,monospace; color:var(--dim);
            border-top:1px solid var(--line); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #panel { width:340px; background:var(--panel); border-left:1px solid var(--line); overflow-y:auto; padding:6px 16px 20px; }
  h1 { font-size:16px; margin:12px 0 4px; letter-spacing:.3px; }
  h2 { font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--dim); margin:18px 0 6px;
       padding-top:12px; border-top:1px solid var(--line); }
  .row { margin:9px 0; }
  .row.hidden { display:none; }
  .lab { display:flex; justify-content:space-between; font-size:13px; margin-bottom:3px; }
  .val { color:var(--dim); font-variant-numeric:tabular-nums; cursor:pointer; }
  input[type=range] { width:100%; accent-color:var(--acc); }
  select { width:100%; background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:5px; }
  label.chk { display:flex; align-items:center; gap:8px; cursor:pointer; }
  input[type=checkbox] { accent-color:var(--acc); width:16px; height:16px; }
  .btns { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
  button { background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px;
           padding:6px 10px; cursor:pointer; font-size:12px; }
  button:hover { border-color:var(--acc); }
  .hint { color:var(--dim); font-size:11px; }
  @media (max-width: 800px) { body { flex-direction:column; overflow:auto; height:auto; }
    #panel { width:auto; border-left:0; } #video { min-height:50vh; } }
</style></head>
<body>
<div id="view">
  <div id="video"><img src="/stream.mjpg" alt="live stream"></div>
  <div id="status">connecting…</div>
</div>
<div id="panel">
  <h1>HermitWatch</h1>
  <div class="hint">Changes apply live and are saved on the Pi. Double-click a value to reset it.</div>
  <div class="btns">
    <button onclick="location.href='/recordings'">Recordings</button>
    <button onclick="api('/api/record', {seconds: 30})" title="Record a 30 s clip now, motion or not">Record 30 s</button>
    <button onclick="window.open('/snapshot.jpg?'+Date.now())">Snapshot</button>
    <button onclick="lockExposure()" title="Switch to manual using the current auto exposure">Lock exposure</button>
    <button onclick="lockWB()" title="Switch to manual using the current auto white balance">Lock white balance</button>
    <button onclick="resetAll()">Reset all</button>
  </div>
  <div id="controls"></div>
</div>
<script>
let schema = [], values = {}, pending = {}, inflight = false, status = {};
const els = {};

async function api(path, body) {
  const opts = body === undefined ? {} :
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(path + ' ' + r.status);
  return r.json();
}

const LOGN = 1000;
const toPos = (f, v) => f.scale === 'log' ? Math.round(LOGN * Math.log(v / f.min) / Math.log(f.max / f.min)) : v;
const fromPos = (f, p) => f.scale === 'log' ? Math.round(f.min * Math.pow(f.max / f.min, p / LOGN)) : +p;

function fmt(f, v) {
  if (f.unit === 'us') return v >= 1000 ? (v / 1000).toFixed(v >= 100000 ? 0 : 1) + ' ms' : v + ' µs';
  const d = f.step >= 1 ? 0 : String(f.step).split('.')[1].length;
  return (+v).toFixed(d) + (f.unit ? ' ' + f.unit : '');
}

function visible(f) {
  if (!f.when) return true;
  const neg = f.when[0] === '!';
  const on = !!values[f.when.replace('!', '')];
  return neg ? !on : on;
}

function build() {
  const root = document.getElementById('controls');
  root.innerHTML = '';
  let group = null;
  for (const f of schema) {
    if (f.group !== group) {
      group = f.group;
      const h = document.createElement('h2'); h.textContent = group; root.appendChild(h);
    }
    const row = document.createElement('div'); row.className = 'row';
    let input, val;
    if (f.type === 'bool') {
      const lab = document.createElement('label'); lab.className = 'chk';
      input = document.createElement('input'); input.type = 'checkbox';
      input.onchange = () => send(f.id, input.checked);
      lab.append(input, document.createTextNode(f.label));
      row.appendChild(lab);
    } else {
      const head = document.createElement('div'); head.className = 'lab';
      const name = document.createElement('span'); name.textContent = f.label;
      val = document.createElement('span'); val.className = 'val'; val.title = 'double-click to reset';
      val.ondblclick = () => send(f.id, f.default);
      head.append(name, val); row.appendChild(head);
      if (f.type === 'select') {
        input = document.createElement('select');
        f.options.forEach((o, i) => input.add(new Option(o, i)));
        input.onchange = () => send(f.id, +input.value);
        val.style.display = 'none';
      } else {
        input = document.createElement('input'); input.type = 'range';
        if (f.scale === 'log') { input.min = 0; input.max = LOGN; input.step = 1; }
        else { input.min = f.min; input.max = f.max; input.step = f.step; }
        input.oninput = () => { const v = fromPos(f, input.value); val.textContent = fmt(f, v); send(f.id, v); };
        input.ondblclick = () => send(f.id, f.default);
      }
      row.appendChild(input);
    }
    root.appendChild(row);
    els[f.id] = { f, row, input, val };
  }
}

function sync() {
  for (const { f, row, input, val } of Object.values(els)) {
    row.classList.toggle('hidden', !visible(f));
    if (document.activeElement === input && f.type === 'range') continue;
    const v = values[f.id];
    if (f.type === 'bool') input.checked = !!v;
    else if (f.type === 'select') input.value = v;
    else { input.value = toPos(f, v); val.textContent = fmt(f, v); }
  }
}

function send(id, v) {
  values[id] = v; pending[id] = v;
  for (const { f, row } of Object.values(els)) row.classList.toggle('hidden', !visible(f));
  flush();
}

async function flush(extra) {
  if (inflight || (!Object.keys(pending).length && !extra)) return;
  inflight = true;
  const body = Object.assign({}, pending, extra || {}); pending = {};
  try { const r = await api('/api/settings', body); values = Object.assign(r.values, pending); }
  catch (e) { document.getElementById('status').textContent = 'error: ' + e.message; }
  finally { inflight = false; }
  if (Object.keys(pending).length) flush(); else sync();
}

async function resetAll() {
  if (!confirm('Reset all camera settings to defaults?')) return;
  pending = {}; await flush({ reset: true });
}

async function lockExposure() {
  const s = await api('/api/status');
  if (!s.ExposureTime) return;
  pending = Object.assign(pending, { ae_auto: false, exposure_us: s.ExposureTime, gain: +s.AnalogueGain.toFixed(1) });
  values = Object.assign(values, pending); sync(); flush();
}

async function lockWB() {
  const s = await api('/api/status');
  if (!s.ColourGains) return;
  pending = Object.assign(pending, { awb_auto: false, red_gain: +s.ColourGains[0].toFixed(2), blue_gain: +s.ColourGains[1].toFixed(2) });
  values = Object.assign(values, pending); sync(); flush();
}

async function pollStatus() {
  try {
    const s = await api('/api/status');
    const parts = [];
    const m = s.motion;
    if (m) {
      if (m.recording) parts.push('<span class="rec">● REC' + (m.forced ? ' (manual)' : '') + '</span>');
      if (m.lighting_change) parts.push('lighting change (ignored)');
      else parts.push((m.moving ? '<span class="mov">MOTION</span> ' : 'motion ') + m.score.toFixed(2) + '%');
    }
    if (s.ExposureTime) parts.push('shutter ' + (s.ExposureTime / 1000).toFixed(1) + ' ms');
    if (s.AnalogueGain) parts.push('gain ' + s.AnalogueGain.toFixed(2));
    if (s.FrameDuration) parts.push((1e6 / s.FrameDuration).toFixed(1) + ' fps');
    if (s.ColourTemperature) parts.push(s.ColourTemperature + ' K');
    if (s.ColourGains) parts.push('R/B ' + s.ColourGains.map(g => g.toFixed(2)).join('/'));
    if (s.Lux !== undefined) parts.push(s.Lux.toFixed(0) + ' lux');
    document.getElementById('status').innerHTML = parts.join('  ·  ') || 'no status';
  } catch (e) { document.getElementById('status').textContent = 'status unavailable'; }
  setTimeout(pollStatus, 1000);
}

(async () => {
  const r = await api('/api/settings');
  schema = r.schema; values = r.values;
  build(); sync(); pollStatus();
})();
</script>
</body></html>"""


RECORDINGS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HermitWatch recordings</title>
<style>
  :root { color-scheme: dark; --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e6e8ee; --dim:#8b93a7; --acc:#e0894a; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 -apple-system,system-ui,sans-serif; }
  header { display:flex; align-items:center; gap:14px; padding:12px 18px; border-bottom:1px solid var(--line);
           position:sticky; top:0; background:var(--bg); z-index:2; }
  header h1 { font-size:16px; margin:0; } header .dim { margin-left:auto; }
  main { padding:6px 18px 40px; max-width:1300px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:1px; color:var(--dim); margin:22px 0 10px; }
  h3 { font-size:13px; margin:16px 0 8px; color:var(--fg); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(210px, 1fr)); gap:12px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .card img { width:100%; aspect-ratio:4/3; object-fit:cover; display:block; background:#000; cursor:pointer; }
  .card .meta { padding:8px 10px; display:flex; justify-content:space-between; align-items:center; gap:6px; font-size:12px; }
  .dim { color:var(--dim); font-size:12px; }
  .rec { color:#ff5a5a; font-weight:600; }
  button, a.btn { background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px;
           padding:5px 9px; cursor:pointer; font-size:12px; text-decoration:none; }
  button:hover, a.btn:hover { border-color:var(--acc); }
  button.del:hover { border-color:#ff5a5a; color:#ff5a5a; }
  .tl { display:flex; gap:14px; align-items:center; background:var(--panel); border:1px solid var(--line);
        border-radius:8px; padding:10px; margin-bottom:10px; }
  .tl img { width:160px; aspect-ratio:4/3; object-fit:cover; border-radius:4px; background:#000; }
  .tl .info { flex:1; } .tl .actions { display:flex; gap:6px; flex-wrap:wrap; }
  #modal { position:fixed; inset:0; background:rgba(0,0,0,.85); display:none; align-items:center; justify-content:center;
           flex-direction:column; gap:10px; z-index:5; }
  #modal video { max-width:94vw; max-height:84vh; background:#000; }
  .empty { color:var(--dim); padding:10px 0; }
</style></head>
<body>
<header><a class="btn" href="/">← Live</a><h1>Recordings</h1><span class="dim" id="disk"></span></header>
<main>
  <h2>Time-lapse</h2><div id="tl"></div>
  <h2>Motion clips</h2><div id="clips"></div>
</main>
<div id="modal" onclick="if(event.target===this)closeModal()">
  <video id="player" controls playsinline></video>
  <div><span class="dim" id="ptitle"></span> <button onclick="closeModal()">Close</button></div>
</div>
<script>
const gb = b => (b / 1073741824).toFixed(b > 1073741824 * 10 ? 0 : 1) + ' GB';
const mb = b => b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';
const hms = t => t ? t.slice(0, 2) + ':' + t.slice(2, 4) + ':' + t.slice(4, 6) : '';
const dur = d => d === undefined ? '' : d >= 60 ? Math.floor(d / 60) + 'm ' + Math.round(d % 60) + 's' : Math.round(d) + 's';
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) alert(j.error || r.status);
  return j;
}

function play(src, title) {
  const v = document.getElementById('player');
  v.src = src; document.getElementById('ptitle').textContent = title;
  document.getElementById('modal').style.display = 'flex'; v.play();
}
function closeModal() {
  const v = document.getElementById('player'); v.pause(); v.removeAttribute('src'); v.load();
  document.getElementById('modal').style.display = 'none';
}

async function delClip(id) { if (confirm('Delete this clip?')) { await post('/api/recordings/delete', { clip: id }); load(); } }
async function delDay(d) { if (confirm('Delete all time-lapse photos and video for ' + d + '?')) { await post('/api/recordings/delete', { timelapse: d }); load(); } }
async function build(d) { await post('/api/timelapse/build', { date: d, fps: +document.getElementById('fps-' + d).value }); load(); }

function render(lib) {
  document.getElementById('disk').textContent = gb(lib.disk.used) + ' used · ' + gb(lib.disk.free) + ' free on SD card';
  const tl = document.getElementById('tl');
  tl.innerHTML = lib.timelapse.length ? lib.timelapse.map(t => `
    <div class="tl">
      ${t.latest ? `<img src="/media/${esc(t.latest)}" loading="lazy">` : ''}
      <div class="info"><b>${esc(t.date)}</b><div class="dim">${t.count} photos · ${hms(t.first)} – ${hms(t.last)}
        ${t.video ? ' · video ' + mb(t.video_size) : ''}</div></div>
      <div class="actions">
        ${t.building ? '<span class="dim">building video…</span>' : `
          <select id="fps-${esc(t.date)}" title="playback speed"><option value="10">10 fps</option><option value="15" selected>15 fps</option><option value="30">30 fps</option></select>
          <button onclick="build('${esc(t.date)}')">${t.video ? 'Rebuild' : 'Build'} video</button>`}
        ${t.video ? `<button onclick="play('/media/timelapse/${esc(t.date)}.mp4', 'Time-lapse ${esc(t.date)}')">Play</button>
                     <a class="btn" href="/media/timelapse/${esc(t.date)}.mp4" download>Download</a>` : ''}
        <button class="del" onclick="delDay('${esc(t.date)}')">Delete</button>
      </div>
    </div>`).join('') : '<div class="empty">No time-lapse photos yet.</div>';

  const byDay = {};
  for (const c of lib.clips) (byDay[c.date] = byDay[c.date] || []).push(c);
  const clips = document.getElementById('clips');
  clips.innerHTML = Object.keys(byDay).length ? Object.entries(byDay).map(([day, list]) => `
    <h3>${esc(day)} <span class="dim">· ${list.length} clip${list.length > 1 ? 's' : ''}</span></h3>
    <div class="grid">${list.map(c => `
      <div class="card">
        ${c.thumb ? `<img src="/media/${esc(c.id)}.jpg" loading="lazy" onclick="play('/media/${esc(c.id)}.mp4', '${esc(c.date)} ${esc(c.time)}')">` : ''}
        <div class="meta">
          <span><b>${esc(c.time)}</b>${c.manual ? ' <span class="dim">manual</span>' : ''} ${c.recording ? '<span class="rec">● REC</span>' : '<span class="dim">' + dur(c.duration) + ' · ' + mb(c.size) + '</span>'}</span>
          ${c.recording ? '' : `<span><a class="btn" href="/media/${esc(c.id)}.mp4" download>⤓</a>
            <button class="del" onclick="delClip('${esc(c.id)}')">✕</button></span>`}
        </div>
      </div>`).join('')}</div>`).join('') : '<div class="empty">No motion clips yet.</div>';
}

async function load() {
  try { render(await (await fetch('/api/recordings')).json()); } catch (e) { console.error(e); }
}
load();
setInterval(() => { if (document.getElementById('modal').style.display !== 'flex') load(); }, 5000);
</script>
</body></html>"""

MEDIA_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".json": "application/json"}


class StreamingHandler(server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the log quiet; slider moves would flood it

    def authorized(self):
        got = self.headers.get("Authorization", "")
        if hmac.compare_digest(got.encode(), EXPECTED_AUTH.encode()):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="HermitWatch", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def send_body(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_body(json.dumps(obj).encode(), "application/json", code)

    def send_file(self, rel):
        """Serve a file under REC_DIR, honouring Range so browsers can seek in videos."""
        path = os.path.realpath(os.path.join(rec.REC_DIR, rel))
        ctype = MEDIA_TYPES.get(os.path.splitext(path)[1])
        if not path.startswith(rec.REC_DIR + os.sep) or not ctype or not os.path.isfile(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        header = self.headers.get("Range", "")
        if header.startswith("bytes="):
            try:
                a, b = header[6:].split(",")[0].strip().split("-")
                if a:
                    start, end = int(a), int(b) if b else size - 1
                else:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
            except ValueError:
                start, end = 1, 0
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", end - start + 1)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=60")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def next_frame(self):
        with output.condition:
            output.condition.wait()
            return output.frame

    def do_GET(self):
        if not self.authorized():
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.send_body(PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/settings":
            with settings_lock:
                self.send_json({"schema": SCHEMA, "values": dict(settings)})
        elif path == "/api/status":
            self.send_json(camera_status())
        elif path == "/recordings":
            self.send_body(RECORDINGS_PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/recordings":
            self.send_json(recorder.library())
        elif path.startswith("/media/"):
            self.send_file(path[len("/media/"):])
        elif path == "/snapshot.jpg":
            self.send_body(self.next_frame(), "image/jpeg")
        elif path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Age", 0)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    frame = self.next_frame()
                    self.wfile.write(b"--FRAME\r\n")
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except Exception as e:
                logging.info("Client %s disconnected: %s", self.client_address, e)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.authorized():
            return
        if self.path not in ("/api/settings", "/api/recordings/delete", "/api/timelapse/build", "/api/record"):
            return self.send_error(404)
        # Requiring JSON forces a CORS preflight, which we never answer,
        # so a malicious web page can't change settings using cached credentials.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json({"error": "Content-Type must be application/json"}, 415)
        length = int(self.headers.get("Content-Length", 0))
        if length > 10_000:
            return self.send_json({"error": "body too large"}, 413)
        try:
            update = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(update, dict):
                raise ValueError("expected an object")
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)

        if self.path != "/api/settings":
            try:
                if self.path == "/api/record":
                    recorder.record_now(update.get("seconds", 30))
                elif self.path == "/api/timelapse/build":
                    recorder.build_timelapse(str(update.get("date", "")), update.get("fps", 15))
                elif "clip" in update:
                    recorder.delete_clip(str(update["clip"]))
                elif "timelapse" in update:
                    recorder.delete_timelapse(str(update["timelapse"]))
                else:
                    raise ValueError("nothing to do")
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json({"ok": True})

        with settings_lock:
            if update.get("reset"):
                settings.clear()
                settings.update(DEFAULTS)
            settings.update(validate(update))
            snapshot = dict(settings)
        try:
            apply_settings(snapshot)
        except Exception as e:
            logging.exception("apply failed")
            return self.send_json({"error": str(e), "values": snapshot}, 500)
        save_settings_soon()
        self.send_json({"values": snapshot})


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    apply_settings(settings)
    recorder = rec.Recorder(
        get_settings=get_settings,
        grab_lores=lambda: picam2.capture_array("lores"),
        latest_jpeg=lambda: output.frame,
        get_circular=lambda: pipeline["circular"],
        camera_lock=camera_lock,
        preroll_s=PREROLL_MS / 1000,
    )
    recorder.start()
    rec.Janitor(get_settings, recorder).start()
    try:
        logging.info("Streaming on http://%s:%d/ (Tailscale only); recordings in %s", BIND, PORT, rec.REC_DIR)
        StreamingServer((BIND, PORT), StreamingHandler).serve_forever()
    finally:
        with camera_lock:
            stop_pipeline()
