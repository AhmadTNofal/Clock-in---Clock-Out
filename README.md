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
- Live camera preview, wall clock, and a large Scan button.
- Automatic direction: your scan records the opposite of your last entry, so
  nobody has to remember which button to press. Explicit **Clock in** /
  **Clock out** buttons are there when needed.
- Repeat scans inside a cooldown window say "already clocked in" instead of
  writing a second row.
- Shows a count of who is on site.
- Space or Enter also triggers a scan, so a cheap USB footswitch wired as a
  keyboard works as the trigger.

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
| `FACE_MIN_PIXELS` | `80` | Minimum detected face width. Set well below what a person standing at the kiosk measures. |
| `FACE_MIN_SHARPNESS` | `45.0` | Blur gate, measured on the aligned crop. Set to roughly half of a good reading from Camera check. |
| `SCAN_FRAMES` / `SCAN_MIN_AGREE` | `3` / `2` | Frames captured per scan, and how many must name the same person. Requiring agreement stops one unlucky frame writing the wrong name into the log. |
| `CLOCK_COOLDOWN_SECONDS` | `90` | Repeat scans in the same direction inside this window are reported, not recorded again. |
| `LIVENESS_REQUIRE_MOTION` | `true` | See the honest assessment below. |
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

98 tests, no MySQL needed — the suite runs against SQLite in memory. With face
photos added (see below) that becomes 104.

### Checking accuracy with your own photos

Six tests are skipped by default because they need real faces, which cannot be
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
capture 3 frames  ──POST──▶  blueprints/kiosk.py
                              └▶ services/recognition.scan()
                                  ├▶ face/engine.py    YuNet detect
                                  │                    SFace embed (128 floats)
                                  ├▶ face/liveness.py  frames must differ
                                  └▶ face/matcher.py   cosine vs every template
                                 services/attendance.py
                                  └▶ alternate direction, apply cooldown  ──▶ attendance_event
                     ◀──JSON──  name, direction, time
```

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
