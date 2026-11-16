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
  /               viewer, camera controls and recordings (ui.html)
  /stream.mjpg    raw MJPEG stream (VLC/ffplay)
  /snapshot.jpg   single JPEG frame
  /api/settings   GET schema+values, POST partial update ({"reset": true} resets)
  /api/status     GET live exposure/gain/white-balance metadata + motion state
  /recordings     redirects to /#recordings
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

UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")


def ui_page():
    """Read the web UI on each request so a deploy only needs ui.html copied."""
    with open(UI_FILE, "rb") as f:
        return f.read()


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
            self.send_body(ui_page(), "text/html; charset=utf-8")
        elif path == "/api/settings":
            with settings_lock:
                self.send_json({"schema": SCHEMA, "values": dict(settings)})
        elif path == "/api/status":
            self.send_json(camera_status())
        elif path == "/recordings":  # old bookmark: recordings now live in a tab
            self.send_response(302)
            self.send_header("Location", "/#recordings")
            self.send_header("Content-Length", "0")
            self.end_headers()
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
