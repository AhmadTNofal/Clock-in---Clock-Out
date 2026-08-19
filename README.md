# Face-recognition clocking system

A clock-in / clock-out system for a small manufacturing site. An employee stands
at a kiosk screen, presses **Scan**, and the system recognises their face and
records the entry. The office gets timesheets and a payroll CSV.

Built with Flask and MySQL. Face recognition runs on two small ONNX models
through OpenCV's DNN module — no PyTorch, no onnxruntime, no dlib build step,
no GPU, and nothing to install beyond `pip install -r requirements.txt`.

---

## What it does

**Kiosk** (`/`, no login)
- **Hands-free by default**: walk up, and you are clocked in or out with no
  button press. See [Hands-free clocking](#hands-free-clocking) below.
- Live camera preview, wall clock, and a large Scan button as a fallback.
- Automatic direction: a scan records the opposite of your last entry, so nobody
  has to remember which button to press. Explicit **Clock in** / **Clock out**
  buttons are there when needed, and override the hands-free interval.
- Repeat scans inside a cooldown window say "already clocked in" instead of
  writing a second row.
- Shows a count of who is on site.
- Space or Enter also triggers a scan, so a cheap USB footswitch wired as a
  keyboard works as the trigger. Escape cancels a pending automatic entry.

**Office** (`/admin`, login required)
- Employees: add, edit, search, deactivate.
- Enrolment: capture several face samples through the browser, with checks that
  they are all the same person and not somebody already enrolled.
- Timesheets: date range, per-employee filter, hours per shift, totals, CSV
  export for payroll.
- Manual entry and voiding for corrections — both fully audited.
- Camera check: measures what your camera actually produces so the recognition
  thresholds can be set from real numbers rather than guesses.

---

## Requirements

- Python 3.11 or newer (developed and tested on 3.14).
- MySQL 8 (or MariaDB 10.6+).
- A webcam on the kiosk machine.
- Roughly 100 MB of disk for the models and virtual environment.

No GPU. Recognition takes a few milliseconds per frame on an ordinary office PC.

---

## Installation

```bash
# 1. Virtual environment
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS

# 2. Dependencies
pip install -r requirements.txt

# 3. Face models (~39 MB, fetched from the OpenCV Model Zoo)
python scripts/fetch_models.py

# 4. Configuration
copy .env.example .env            # cp on Linux / macOS
```

Now edit `.env`. At minimum set the MySQL credentials, and generate real secrets:

```bash
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('KIOSK_TOKEN=' + secrets.token_urlsafe(32))"
```

The application refuses to start in production mode while either is still a
placeholder.

Create a MySQL user and the database:

```sql
CREATE DATABASE clocking CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'clocking'@'localhost' IDENTIFIED BY 'a-strong-password';
GRANT ALL PRIVILEGES ON clocking.* TO 'clocking'@'localhost';
FLUSH PRIVILEGES;
```

Then create the tables and your first administrator:

```bash
python scripts/init_db.py --admin office
```

`scripts/init_db.py --create-database --root-user root` will also issue the
`CREATE DATABASE` for you if you would rather not do it by hand. The script
never drops anything, so it is safe to re-run.

Start it:

```bash
python run.py            # development, http://127.0.0.1:5000
python wsgi.py           # production via Waitress, port 8000
```

Sign in at `/login`, add an employee, enrol their face, then open `/` on the
kiosk machine.

`schema.sql` holds the MySQL DDL if a DBA wants to review or apply it directly.

### If the database is not on this machine

A managed database (DigitalOcean, RDS, Azure) is reached across the public
internet. Face templates are biometric data and the password travels on the same
connection, so **the link must be encrypted**.

This is handled for you: `MYSQL_SSL_MODE` defaults to `verify-identity` for any
non-local host, and to `disabled` for `localhost`. Production mode refuses to
start if you point it at a remote database with TLS switched off.

| `MYSQL_SSL_MODE` | Behaviour |
|---|---|
| *(blank)* | Automatic: `disabled` for localhost, `verify-identity` otherwise. Recommended. |
| `verify-identity` | Encrypt, and verify the server certificate and hostname. |
| `required` | Encrypt, but do not verify the certificate. Use only if verification fails and you accept the risk. |
| `disabled` | No encryption. Acceptable only for a database on this machine. |

If your provider issues its own CA certificate, download it and set
`MYSQL_SSL_CA` to its path — for DigitalOcean it is `ca-certificate.crt` on the
database's Connection Details page.

To confirm a live connection really is encrypted:

```sql
SHOW STATUS LIKE 'Ssl_version';   -- should report TLSv1.2 or TLSv1.3, not blank
```

Do not take an absence of errors as proof: several plausible PyMySQL settings
(`ssl={}`, `ssl_verify_cert=False`, `ssl_disabled=False`) connect in **plaintext**
while appearing to enable TLS. `app/config.py` uses the combination that was
checked against a real server, and `tests/test_config.py` guards it.

---

## Hands-free clocking

By default nobody touches anything: the kiosk notices somebody arrive,
recognises them, and records the entry.

```
somebody arrives          recognised                countdown ends
      │                       │                           │
      ▼                       ▼                           ▼
   LOOKING ──────────▶  "Sam Fletcher                 entry written
 (presence seen)         Clocking IN  4"              to the database
                          [ Cancel ]
                              │
                     walk away / press Cancel
                              │
                              ▼
                       nothing recorded
```

### It works as a toggle

Clocked in? The next time the kiosk sees you, it clocks you out. Clocked out? It
clocks you in. Nothing to press, no direction to choose.

What stops it clocking you over and over is **absence, not elapsed time**: after
an entry, the kiosk waits until you have walked out of the camera's view before it
will clock you again. That is the whole rule, and it matches what people expect
from a switch:

```
        arrive  ─────────────▶  clocked IN
                                     │
        stand there  ──────────▶  nothing happens
        (screen: "All set — step away from the camera")
                                     │
        walk away  ────────────▶  kiosk re-arms (scene reads empty ~1s)
                                     │
        come back  ────────────▶  clocked OUT
```

Gating on time cannot do this. A long interval blocks you from genuinely clocking
out; a short one clocks a stationary person in and straight back out again while
they read the screen. Absence has neither problem.

Two settings control it:

- `AUTO_REQUIRE_DEPARTURE` (default `true`) — the rule above.
- `AUTO_DEPARTURE_MS` (default 900) — how long the scene must read empty to count
  as having left. Long enough to ignore somebody shifting their weight.

`AUTO_MIN_INTERVAL_SECONDS` is now only a **backstop** (default 10 s) for cases
where the browser's departure check cannot be trusted — a reloaded page, a second
kiosk, or a camera that cannot distinguish an empty doorway. Keep it short: a long
value goes back to blocking genuine clocking out. The test suite fails if it is
set above 30 s.

#### If the camera never sees an empty scene

Departure gating on its own fails closed. Point the kiosk at a desk you sit at, or
a doorway that is never clear, and the scene never reads empty — so the kiosk
clocks you once and then never again. That is a real trap, and it is why
`AUTO_REARM_SECONDS` (default 30) exists: the kiosk re-arms after that long
regardless, and the screen counts it down ("All set — step away from the camera
(ready again in 12s)") so the wait is never a mystery.

Departure remains the fast path — walk away and it re-arms in about a second. The
timer is only there so it can never get stuck. The trade-off runs the other way:
somebody permanently in view is offered a clock every `AUTO_REARM_SECONDS`, with
the countdown as the guard, so do not set it very low. `0` disables the fallback
and relies on departure alone.

### Why there is a countdown

The obvious design — recognise a face, write the row immediately — has a nasty
failure mode. Walk past the kiosk mid-shift and it clocks you *out*, quietly
losing the rest of your day's pay. Two things stand between that and a wrong
entry:

1. **Nothing is written until the countdown finishes.** Recognition and recording
   are separate steps (`/identify` then `/commit`). Walking away, or pressing
   **Cancel** or Escape, means no entry ever existed — nothing to undo and
   nothing for the office to correct.
2. **A button press always wins.** If you genuinely arrive and leave straight
   away, press **Clock out** — a pressed button states intent, so it overrides
   the interval.

**Be aware of the trade-off.** Because toggling is deliberately easy, the
countdown is doing most of the safety work: somebody who walks up to the camera
mid-shift, for any reason, *will* be offered a clock-out, and only the countdown
stops it. That is the price of the kiosk behaving like a switch.

This is a question of siting more than settings. A kiosk in a doorway people stop
at is fine; one in a corridor people walk through is not, because they will be
clocked in passing. If you cannot avoid that, lengthen `AUTO_CONFIRM_SECONDS` so
there is more time to cancel, raise `AUTO_MIN_INTERVAL_SECONDS`, or set
`KIOSK_AUTO_MODE=false` and use the buttons.

Check the timesheet report for a week after go-live: unexpectedly short shifts, or
rows flagged "No clock-out recorded", are the symptom of accidental clocking.

Automatic entries are stored with `method = "auto"`, so a payroll query can tell
them from a deliberate scan (`face`) or an office correction (`manual`).

### Speed, and how far away it works

Both were measured rather than guessed (`FACE_DETECT_MAX_SIDE` etc. are all
tunable in `.env`):

| | Time to show the name | Time to write the entry |
|---|---|---|
| Before | 1.03 s | 5.03 s |
| Now | **0.54 s** | **2.54 s** |

The interesting part is where the time was *not* going. Recognition itself takes
about 42 ms; what cost a second was grabbing three frames 320 ms apart before the
request even left the browser, and then a four-second countdown. So the wins were
two frames instead of three, a 2 s countdown instead of 4 s, and a faster
presence check — not faster maths.

That also means detection range was available almost free. Detection costs ~11 ms
at 640 px and ~28 ms at 960 px, both irrelevant beside the capture time, so the
frames are now 960 px wide. **The browser was the real limit**: it downscaled
every upload to 640 px, and no server setting can recover detail that has already
been thrown away. `CAPTURE_MAX_WIDTH` controls that, and the test suite asserts it
is never below `FACE_DETECT_MAX_SIDE`.

`FACE_MIN_PIXELS` dropped from 80 to 55 on measured evidence: on real photographs
a face is still matched at ~0.92 cosine (against ~0.10 for a different person)
down to about 47 px wide, and YuNet still detects one at 30 px. 55 keeps headroom
for a soft webcam frame. Together — 1.5× the pixels and a 1.45× lower floor — a
face is recognised at roughly **twice** the distance it used to be.

If people are still not picked up far enough away, in order of effect: check the
camera actually delivers more than 720p (`CAPTURE_MAX_WIDTH` cannot invent
detail), raise `CAPTURE_MAX_WIDTH` and `FACE_DETECT_MAX_SIDE` together to 1280,
then lower `FACE_MIN_PIXELS` towards 45. Use **Camera check** to see the real
face-pixel figures at the distance people actually stand.

### Seeing what the kiosk is thinking

Open the kiosk with `?debug=1` — for example `http://localhost:8000/?debug=1` — and
it overlays its live state: the presence score against the threshold, whether it is
armed or latched (and how long until it re-arms), the last reply from the server,
and the miss count. Tuning `AUTO_PRESENCE_THRESHOLD` by guesswork is miserable;
this shows the number you are trying to set. It is off unless asked for, so the
shop-floor screen stays clean.

### When it cannot recognise somebody

A single miss is normal while somebody walks up, so the screen stays quiet rather
than flickering. But after a few consecutive misses it says what would help —
"come a little closer", "hold still", "one at a time", "not recognised, try the
Scan button". Failing silently would leave somebody watching a screen that looks
broken.

### Bystanders

A shop floor is busy, and refusing every frame containing two faces would make
hands-free clocking unusable. Instead the nearest face wins, provided it is at
least `FACE_DOMINANT_RATIO` (default 1.35) times wider than the next — somebody
clearly closest to the camera is the person using the kiosk. If two people are
equally close, nobody is clearly "at" the kiosk, so it refuses and asks them to
step up one at a time rather than guessing.

The stricter one-person-only rule still applies to button presses.

### It does not run recognition all day

Face recognition on every frame, all day, for an empty doorway would be a waste
of the machine. So the browser answers the cheap question first — "has anybody
arrived?" — by comparing a small greyscale frame against a reference image of
the empty scene, and only then asks the server the expensive question, "who is
this?".

Comparing against the *empty scene* rather than the previous frame is deliberate:
somebody standing still, waiting to be clocked, produces almost no frame-to-frame
change but a large difference from the empty doorway — and that is exactly the
person we must not miss. The reference is re-learned whenever the scene reads as
empty, so daylight changing through the workshop windows does not slowly become
a permanent false trigger.

If the kiosk keeps waking at shadows or passing forklifts, raise
`AUTO_PRESENCE_THRESHOLD`. If it ignores people who approach slowly, lower it.

### Turning it off

Set `KIOSK_AUTO_MODE=false` and the kiosk reverts to press-to-scan. The badge at
the top of the kiosk screen always shows which mode is active.

---

## Important: the camera needs a secure origin

Browsers only grant camera access on a *secure origin*. In practice:

- `http://localhost` and `http://127.0.0.1` **work**.
- `https://anything` **works**.
- `http://192.168.1.50` (a plain-http LAN address) is **refused** — the camera
  will not start and the page will say so.

For a small site, cheapest first:

1. **Run the browser on the server machine.** Point the kiosk at
   `http://localhost:8000`. Nothing else to configure. This is the recommended
   setup for a single kiosk.
2. **Reverse proxy with a certificate.** Put Caddy or nginx in front with an
   internal or self-signed certificate, and trust that certificate on the kiosk
   machine. Needed if the kiosk is a separate device from the server.

---

## Tuning recognition

Every threshold lives in `.env`. Before changing any of them, open
**Camera check** in the office pages and measure your actual camera and lighting
— it reports face size, sharpness, motion and match scores without recording
anything.

| Setting | Default | What it does |
|---|---|---|
| `FACE_MATCH_THRESHOLD` | `0.40` | Cosine similarity needed to accept a match. Raise towards `0.45` to reduce the chance of a wrong match; lower it if genuine employees are being refused. OpenCV's reference figure for this model is `0.363`. |
| `FACE_MATCH_MARGIN` | `0.05` | The best match must beat the runner-up by this much. Guards against look-alikes; raising it makes the system say "see the office" rather than guess. |
| `FACE_MIN_PIXELS` | `55` | Minimum detected face width — the main control on how far away recognition works. Measured floor is ~47px; below ~45 accuracy falls away. |
| `FACE_MIN_SHARPNESS` | `45.0` | Blur gate, measured on the aligned crop. Set to roughly half of a good reading from Camera check. |
| `SCAN_FRAMES` / `SCAN_MIN_AGREE` | `3` / `2` | Frames captured per scan, and how many must name the same person. Requiring agreement stops one unlucky frame writing the wrong name into the log. |
| `CLOCK_COOLDOWN_SECONDS` | `90` | Repeat scans in the same direction inside this window are reported, not recorded again. |
| `LIVENESS_REQUIRE_MOTION` | `true` | See the honest assessment below. |
| `KIOSK_AUTO_MODE` | `true` | Hands-free clocking. `false` reverts to press-to-scan. |
| `AUTO_CONFIRM_SECONDS` | `2` | Cancellable countdown before an automatic entry is written. `0` records instantly (not advised — it removes the only guard against being clocked out in passing). |
| `AUTO_REQUIRE_DEPARTURE` | `true` | Require the person to leave the camera's view before they can be clocked again. This is what makes the kiosk a toggle. |
| `AUTO_DEPARTURE_MS` | `900` | How long the scene must read empty to count as having left. |
| `AUTO_REARM_SECONDS` | `30` | Re-arm after this long even if the scene never reads empty. Stops the kiosk getting permanently stuck when the camera can always see somebody. `0` disables it. |
| `AUTO_MIN_INTERVAL_SECONDS` | `10` | Backstop only, for when the departure check cannot be trusted. Keep it short — a long value blocks genuine clocking out. |
| `AUTO_PRESENCE_THRESHOLD` | `7.0` | How much the scene must change to count as somebody arriving. Raise if it wakes at shadows. |
| `FACE_DOMINANT_RATIO` | `1.35` | How much nearer the kiosk user must be than a bystander behind them. |
| `CAPTURE_MAX_WIDTH` | `960` | Width the browser downscales to before upload. The binding constraint on range — the server cannot recover detail discarded here. |
| `FACE_DETECT_MAX_SIDE` | `960` | Width the server detects at. Keep equal to `CAPTURE_MAX_WIDTH`. |
| `AUTO_SCAN_FRAMES` | `2` | Frames per hands-free check. Two is the liveness minimum; capture time dominates responsiveness. |
| `AUTO_FRAME_GAP_MS` | `300` | Gap between those frames. Shortening it makes live people look like photographs to the liveness check. |
| `TIMEZONE` | `Europe/London` | Used for day boundaries and all displayed times. |

For good recognition, enrol people **at the kiosk, under the kiosk's lighting**,
with a few different head angles, and including safety glasses or hair nets if
those are normally worn.

---

## What the liveness check is and is not

`app/face/liveness.py` compares the aligned face crops across the frames of one
scan and requires that something about the face changed. A live face is never
perfectly still; a photo held up to the camera produces near-identical crops.

**It stops:** a still photo on paper or a phone screen, and a frozen or stalled
camera feed.

**It does not stop:** a video of the employee played back on a screen, a
convincing mask, or a determined attacker. It is a deterrent, not certified
anti-spoofing.

For a kiosk inside a workshop, in sight of a supervisor, that is usually the
right trade-off. If your risk assessment says otherwise, the honest options are
a supervised kiosk, a second factor alongside the face, or a camera with genuine
depth or infra-red liveness hardware. Do not assume this code is more than it is.

---

## Data protection

Face templates are numeric vectors, not photographs: the captured images are
used to compute a template and then discarded. **This is still biometric data,
and under UK GDPR biometric data used to identify someone is special-category
data** (Article 9). Before you enrol a single employee:

- Identify your lawful basis, and an Article 9 condition. Consent from an
  employee is often not considered freely given, because of the imbalance of
  power in an employment relationship — so consent is usually the *weaker*
  choice here, not the safer one.
- Complete a Data Protection Impact Assessment. For biometric monitoring of
  staff, the ICO regards a DPIA as required, not optional.
- Offer a genuine, non-detrimental alternative for anyone who objects (the
  manual-entry feature exists partly for this).
- Update your privacy notice, retention schedule and records of processing.
- Set a retention period for attendance data and face templates, and apply it.

**This section is a pointer, not legal advice.** Biometric monitoring of staff
is an area where the ICO has taken enforcement action against employers. Have
your DPIA reviewed by someone qualified — a data protection adviser or
employment solicitor — before go-live.

Practical measures already in the code: face data lives only in your MySQL
database and never leaves the server; the admin area requires a login;
`Remove face data` on an employee deletes their templates immediately; and
deactivating an employee drops them from the recognition index.

---

## Running the tests

```bash
python -m pytest
```

143 tests, no MySQL needed — the suite runs against SQLite in memory. With face
photos added (see below) that becomes 151.

### The kiosk JavaScript

The hands-free countdown lives in browser code, so `tests/js/kiosk_harness.js`
stubs the DOM, camera and network and drives the real `kiosk.js` with fake
timers, checking that an empty doorway produces no requests, that nothing is
committed while the countdown runs, that letting it finish commits exactly once,
that **Cancel prevents the commit**, that standing still does not clock you
repeatedly while leaving and returning does, that a camera which never sees an
empty scene still recovers, and that repeated misses produce an
actionable hint. `pytest` runs it automatically when
Node is installed, and skips it otherwise. To run it directly:

```bash
node tests/js/kiosk_harness.js
```

It earned its keep immediately: it caught the recognition poll timer not being
stopped when a countdown began, which meant that once the screen returned to
idle the stale poll kept calling `/identify` with nobody in front of the camera
and started a fresh countdown that then committed an entry.

### Checking accuracy with your own photos

Eight tests are skipped by default because they need real faces, which cannot be
committed to a repository. To enable them, drop a few photos into
`tests/fixtures/faces/`:

```
tests/fixtures/faces/
    sam_1.jpg     # two or more photos of one person
    sam_2.jpg
    ada_1.jpg     # and at least one of somebody else
```

Photos sharing the prefix before the first underscore are treated as the same
person. That folder is git-ignored — the photos are personal data and must stay
local.

With those in place, `pytest` additionally verifies end to end that:

- enrolment, clock-in, clock-out and CSV export all work through real HTTP calls
  with the real models;
- **a different person is not recognised** as an enrolled employee;
- **a still photo held up to the camera is refused** by the liveness check;
- the same face cannot be enrolled twice under two payroll references;
- two photos of one person score above the match threshold, and two different
  people score below it.

That last group is worth running with photos of your own staff before go-live —
it is the closest thing to a site acceptance test.

---

## How it works

```
Browser (kiosk)                  Flask                        MySQL
--------------                   -----                        -----
presence check                                               (nothing yet)
 (browser only, cheap)
      │ somebody there
      ▼
capture 3 frames  ──POST /identify─▶  blueprints/kiosk.py
                                       └▶ services/recognition.scan()
                                           ├▶ face/engine.py    YuNet detect
                                           │                    SFace embed (128 floats)
                                           ├▶ face/liveness.py  frames must differ
                                           └▶ face/matcher.py   cosine vs every template
                  ◀──JSON────────────  who, direction, signed token
      │
      │ countdown; Cancel stops here
      ▼
     ──POST /commit────────────────▶  verify signature
                                       services/attendance.py
                                        └▶ apply interval  ──────────▶ attendance_event
                  ◀──JSON────────────  name, direction, time
```

The button path (`/scan`) collapses both steps into one request, because a press
already states intent.

| Path | Purpose |
|---|---|
| `app/face/engine.py` | Detection, alignment, embedding, quality gates. |
| `app/face/matcher.py` | The in-memory index and the threshold/margin/voting rules. |
| `app/face/liveness.py` | The presentation-attack deterrent. |
| `app/services/recognition.py` | Ties the engine and index to Flask and MySQL. |
| `app/services/attendance.py` | Alternation and cooldown rules. |
| `app/services/enrolment.py` | Enrolment with same-person and duplicate checks. |
| `app/services/timesheet.py` | Pairing events into shifts, totals, CSV, timezones. |
| `app/blueprints/` | Kiosk, auth and admin routes. |
| `app/security.py` | Rate limiting and the kiosk shared secret. |

Design decisions worth knowing:

- **Timestamps are stored in UTC** and converted to local time only for display,
  so the BST/GMT change cannot corrupt stored data. A shift is credited to the
  local date it started, keeping night shifts on one line.
- **The event log is append-only.** A wrong entry is voided, never overwritten,
  and a correction is added — so the audit trail survives.
- **Unpaired entries are flagged, never guessed.** If somebody forgot to clock
  out, the timesheet says so and leaves the hours blank. Inventing a leaving
  time would put a wrong figure into someone's pay.
- **Matching is a linear scan** — one matrix-vector product against every
  template. At small-manufacturer headcount this is sub-millisecond, and it
  avoids an approximate-nearest-neighbour index that would need maintaining.
- **The rate limiter is in-process**, not Redis. This runs as one Waitress
  process on an office PC; an extra service to install and back up would cost
  more than it adds.

---

## Running as a Windows service

Waitress runs happily under [NSSM](https://nssm.cc/):

```
nssm install ModuflexClocking "D:\Clock in Clock Out\Clock-in---Clock-Out\.venv\Scripts\python.exe"
nssm set    ModuflexClocking AppDirectory "D:\Clock in Clock Out\Clock-in---Clock-Out"
nssm set    ModuflexClocking AppParameters "wsgi.py"
nssm start  ModuflexClocking
```

Monitor `GET /healthz` — it returns 503 if the database is unreachable or the
face models are missing.

**Back up the MySQL database.** The face templates live there, and losing them
means re-enrolling everybody.

```bash
mysqldump -u clocking -p clocking > clocking-backup.sql
```

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Camera unavailable" on the kiosk | Not a secure origin. Use `localhost` or put HTTPS in front — see above. |
| Kiosk clocks people out as they walk past | Raise `AUTO_MIN_INTERVAL_SECONDS`, or move the camera so it only sees people who stop at it. |
| Kiosk keeps waking with nobody there | Raise `AUTO_PRESENCE_THRESHOLD`. |
| Hands-free never triggers | Lower `AUTO_PRESENCE_THRESHOLD`; check the mode badge says "Automatic"; check **Camera check** sees a face. |
| Not picked up until you are close | Raise `CAPTURE_MAX_WIDTH` and `FACE_DETECT_MAX_SIDE` to 1280, then lower `FACE_MIN_PIXELS` towards 45. Check the camera is better than 720p. |
| Live people rejected as photographs | The liveness gap is too short or the camera too noisy. Raise `AUTO_FRAME_GAP_MS`, or lower `LIVENESS_MIN_MOTION` (weakens photo protection). |
| Clocked in but wanted to stay in | Press **Cancel** during the countdown. |
| Screen says "step away from the camera" | Working as intended — you have been clocked, and it will not clock you again until you have left the view. |
| Clocks once, then never again | The camera can always see somebody, so the departure check never clears. It now frees itself after `AUTO_REARM_SECONDS`; open the kiosk with `?debug=1` to watch the presence score and latch state live. Move the camera so it sees an empty scene, or raise `AUTO_PRESENCE_THRESHOLD`. |
| Will not clock me out when I come back | The kiosk has not seen the doorway empty. Check **Camera check**: the presence score must drop below the threshold when nobody is there. Lower `AUTO_DEPARTURE_MS` or raise `AUTO_PRESENCE_THRESHOLD`. |
| "Face recognition is not set up on this server" | Run `python scripts/fetch_models.py`. |
| "Face not recognised" for a known employee | Re-enrol at the kiosk under kiosk lighting. Check Camera check readings; consider lowering `FACE_MATCH_THRESHOLD` slightly. |
| "Could not tell you apart from another record" | Two enrolments are too similar — often the same person enrolled twice. Check the employee list, remove the duplicate. |
| "Live camera check failed" | Someone is very still, or the feed has frozen. Raise nothing yet: check the feed first, then consider lowering `LIVENESS_MIN_MOTION`. |
| Refuses to start: "placeholder SECRET_KEY" | Set real secrets in `.env`. |
| Refuses to start: "would cross the network unencrypted" | Remote database with TLS off. Set `MYSQL_SSL_MODE=verify-identity`. |
| `SSL: CERTIFICATE_VERIFY_FAILED` connecting to the database | Your provider uses its own CA. Set `MYSQL_SSL_CA` to its certificate, or fall back to `MYSQL_SSL_MODE=required`. |
| `ZoneInfoNotFoundError` | `pip install tzdata` — Windows has no system timezone database. |

---

## Licence

See `LICENSE`.
