# HermitWatch

A Raspberry Pi camera server for watching hermit crabs. It streams live video to
your browser, gives you full control over the camera, and **records
automatically when the crabs move**: motion-triggered video clips (including the
seconds *before* the movement started) and time-lapse photos, with a built-in
log, charts and player to review what happened overnight.

Everything runs on the Pi in a single Python process. You reach it privately over
[Tailscale](https://tailscale.com); nothing is exposed to your local network or
the internet.

## Features

- **Live stream** in any browser (MJPEG), plus raw stream and snapshot URLs for VLC/ffplay.
- **Camera controls** that apply instantly and persist across restarts:
  brightness, contrast, saturation, sharpness, noise reduction; auto or manual
  exposure (EV, metering, shutter up to 1 s, gain); auto or manual white balance;
  anti-flicker (50/60 Hz); frame rate; digital zoom and pan; flip.
  *Lock exposure* / *Lock white balance* freeze whatever auto mode picked.
- **Motion detection** tuned for slow animals: compares each frame with one about
  1 s older on a low-resolution stream, and ignores whole-frame lighting changes.
- **Motion-triggered clips** (H.264 MP4) with 5 s of pre-roll; they keep recording
  for a configurable time after motion stops and split very long activity into
  several clips.
- **Time-lapse photos**, either only while crabs are moving or continuously,
  turned into an MP4 per day with one click.
- **Recordings tab**: day picker, summary (clips, time recorded, busiest hour),
  a timeline of every clip, a recorded-per-hour chart, and a recording log.
  Clips and time-lapses play in the main video area with Prev/Next and
  **Back to live**.
- **Storage management**: oldest recordings are deleted past a size limit
  (default 20 GB); recording pauses below 1 GB free.
- **Private by default**: listens only on the Pi's Tailscale address and requires
  a username and password.

## Hardware

- Raspberry Pi 4 (developed on a Pi 4 Model B; other models with the hardware
  H.264 encoder should work)
- A Raspberry Pi camera module (developed with the OV5647 / Camera Module v1)
- Hermit crabs are mostly active at night. To record in the dark, use a **NoIR**
  camera with an infrared light; a regular camera sees very little without light.

## Requirements (on the Pi)

Raspberry Pi OS (developed on Debian 13 "trixie") with:

```bash
sudo apt install python3-picamera2 python3-av python3-numpy ffmpeg
```

Install [Tailscale](https://tailscale.com/download/linux) and join the Pi to your
tailnet (`sudo tailscale up`). HermitWatch refuses to start without a Tailscale
address.

## Setup

1. **Copy the files** to the Pi's home directory (or clone this repo there):

   ```bash
   scp cam_stream.py recorder.py ui.html pi@<your-pi>:~/
   ```

2. **Create the login** on the Pi: one line, `username:password`, readable only by you:

   ```bash
   ssh pi@<your-pi>
   umask 077; printf '%s' 'myname:my-password' > ~/.cam_stream_auth
   ```

3. **Start it**, either with the helper script from your computer (below) or on the Pi:

   ```bash
   nohup python3 ~/cam_stream.py > ~/cam_stream.log 2>&1 < /dev/null &
   ```

4. Open **`http://<your-pi>:8000/`** from any device on your tailnet and sign in.

## The helper script

`hermitwatch.sh` controls the Pi from your computer over SSH:

| Command | What it does |
|---|---|
| `./hermitwatch.sh start` | Start (no-op if already running) and confirm it's streaming |
| `./hermitwatch.sh stop` | Stop (forces it after 10 s) |
| `./hermitwatch.sh restart` | Stop, then start |
| `./hermitwatch.sh deploy` | Copy `cam_stream.py`, `recorder.py`, `ui.html` to the Pi, then restart |
| `./hermitwatch.sh status` | Show whether it's running, with the URL |
| `./hermitwatch.sh logs` | Follow the server log |

It targets `pi@raspberrypi` by default. To point it at your Pi, put `user@host` in a
`.hermitwatch-host` file next to the script (git ignores it, so your host name stays
out of the repo), or set `HERMITWATCH_HOST=user@host` for one command:

```bash
echo 'pi@my-pi' > .hermitwatch-host
```

## Using the web page

- **Camera tab**: all controls are grouped as Image, Exposure, Colour, Framing,
  Recording and Motion detection. Changes apply live. Double-click a value to reset
  it. *Record 30 s* records a clip on demand.
- **Status bar** (under the video): `● REC` while recording, `MOTION` and the
  motion score, plus shutter, gain, frame rate, colour temperature and light level.
- **Recordings tab**: pick a day to see the summary, the timeline (blue = motion,
  orange = manual) and the per-hour chart, then click any clip in the timeline or
  log to play it. Use **Prev / Next** (or Shift + ←/→) to step through clips, and
  **← Back to live** (or Esc) to return to the stream. Build, play, download or
  delete the day's time-lapse.

## Tuning motion detection

Watch the motion score in the status bar while the crabs are still and while they move.

| Setting | Default | Raise it if… | Lower it if… |
|---|---|---|---|
| Minimum moving area | 0.3 % | it records when nothing is moving | it misses small or distant crabs |
| Pixel change threshold | 12 | camera noise triggers it (dim light, high gain) | slow or low-contrast movement is missed |
| Compare frames this far apart | 1 s | — | — (raise it to catch very slow crawling) |
| Keep recording after motion stops | 10 s | clips get cut into many short pieces | clips run on too long |

Changes affecting more than 40 % of the frame at once (a lamp switching on,
auto-exposure jumps) are treated as lighting changes and ignored.

A long manual shutter lowers the frame rate (a 1 s shutter means 1 fps). Clips
still record correctly but look like a slideshow, and motion checks run once
per frame. For smooth video, add light and use a shorter shutter.

## Where things are stored (on the Pi)

| Path | Contents |
|---|---|
| `~/.cam_stream_auth` | `username:password` for the web page (keep it `chmod 600`) |
| `~/.cam_stream_settings.json` | camera and recording settings |
| `~/hermitwatch/clips/YYYY-MM-DD/HHMMSS.mp4` | motion clips (`.jpg` thumbnail, `.json` start/end/duration/peak) |
| `~/hermitwatch/timelapse/YYYY-MM-DD/*.jpg` | time-lapse photos |
| `~/hermitwatch/timelapse/YYYY-MM-DD.mp4` | built time-lapse video |
| `~/cam_stream.log` | server log |

Environment variables: `PORT` (8000), `WIDTH`/`HEIGHT` (1296×972), `BIND`
(defaults to the Tailscale IP), `AUTH_FILE`, `SETTINGS_FILE`, `REC_DIR`.

## How it works

```
                        ┌─ main 1296×972 ─┬─ MJPEG encoder ──► live stream / snapshots / time-lapse photos
camera (picamera2) ─────┤                 └─ H.264 encoder ──► 5 s circular buffer ──► MP4 clip when motion
                        └─ lores 320×240 ──► motion detector (4×4 block average, diff vs ~1 s ago)
```

- `cam_stream.py`: camera pipeline, settings, HTTP server and API.
- `recorder.py`: motion detection, clip and time-lapse recording, library, storage cleanup.
- `ui.html`: the web page (read on each request, so a deploy only has to copy it).

The H.264 keyframe interval follows the frame rate (about one per second), so
clips can start promptly even at 1 fps.

### HTTP API

All endpoints need the same login. POSTs must be `application/json`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/stream.mjpg` | live MJPEG stream |
| GET | `/snapshot.jpg` | current frame |
| GET/POST | `/api/settings` | schema + values / partial update (`{"reset": true}` restores defaults) |
| GET | `/api/status` | exposure, gain, white balance, motion and recording state |
| POST | `/api/record` | record now, e.g. `{"seconds": 30}` |
| GET | `/api/recordings` | clips, time-lapse days, disk usage |
| POST | `/api/recordings/delete` | `{"clip": "clips/2026-11-16/213000"}` or `{"timelapse": "2026-11-16"}` |
| POST | `/api/timelapse/build` | `{"date": "2026-11-16", "fps": 15}` |
| GET | `/media/<path>` | recorded files (supports Range for seeking) |

## Security notes

- The server binds only to the Tailscale address, so it can't be reached from your
  home Wi-Fi or the internet. Tailscale encrypts the traffic.
- The login uses HTTP Basic Auth. It's safe here because the connection is
  already encrypted by Tailscale; don't expose port 8000 any other way (and never
  forward it on your router).
- Requiring JSON on POST requests stops other websites from changing settings
  through your logged-in browser.
- The password lives only in `~/.cam_stream_auth` on the Pi; it isn't stored in
  this repository.

## Troubleshooting

- **Won't start: "Tailscale IP not found"**: run `tailscale ip -4` on the Pi
  and bring Tailscale up.
- **Won't start: "Auth file … missing"**: create `~/.cam_stream_auth` (Setup step 2).
- **No login prompt and a blank page**: some embedded browsers don't show the
  Basic Auth dialog; use Safari, Chrome or Firefox.
- **Records constantly**: raise *Minimum moving area* or *Pixel change threshold*;
  check the status bar for `lighting change` flicker and set *Anti-flicker* to
  your mains frequency.
- **Never records**: lower *Minimum moving area*; make sure *Record video clips
  on motion* is on; use *Record 30 s* to confirm recording itself works.

## License

[GNU General Public License v3.0](LICENSE)
