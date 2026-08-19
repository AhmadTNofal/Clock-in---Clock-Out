/* Kiosk screen behaviour.
 *
 * Hands-free clocking is a small state machine:
 *
 *   IDLE ---- somebody arrives ----> LOOKING ---- recognised ----> CONFIRMING
 *     ^                                 |                              |
 *     |                            not recognised                  countdown
 *     |                                 |                        ends / cancelled
 *     +------------- RESULT <-----------+<-----------------------------+
 *
 * The CONFIRMING pause is a safety feature, not decoration. Without it, walking
 * past the camera two hours into a shift would clock you out; with it, the
 * screen says "Clocking OUT in 2..." and the person can cancel or simply walk
 * away. Nothing is written to the database until the countdown finishes.
 *
 * The buttons remain available throughout, and take priority over the automatic
 * path - useful for the person who really is leaving straight after arriving.
 */
(function () {
    "use strict";

    var config = window.KIOSK_CONFIG;
    var video = document.getElementById("kiosk-video");
    var hint = document.getElementById("kiosk-hint");
    var scanBtn = document.getElementById("scan-btn");
    var scanIn = document.getElementById("scan-in");
    var scanOut = document.getElementById("scan-out");
    var cancelBtn = document.getElementById("cancel-btn");
    var resultBox = document.getElementById("kiosk-result");
    var nameEl = document.getElementById("result-name");
    var actionEl = document.getElementById("result-action");
    var timeEl = document.getElementById("result-time");
    var detailEl = document.getElementById("result-detail");
    var onsiteEl = document.getElementById("onsite");
    var clockEl = document.getElementById("kiosk-clock");
    var dateEl = document.getElementById("kiosk-date");
    var modeEl = document.getElementById("kiosk-mode");

    var capture = new window.FaceCapture(video, { maxWidth: config.captureMaxWidth });
    var presence = new window.PresenceDetector(capture, {
        threshold: config.presenceThreshold
    });

    var STATE = { IDLE: "idle", LOOKING: "looking", CONFIRMING: "confirming", RESULT: "result" };
    var state = STATE.IDLE;
    var busy = false;              /* a request is in flight */
    var pending = null;            /* { token, employee, direction } */
    var countdownTimer = null;
    var resultTimer = null;
    var presenceTimer = null;
    var lookTimer = null;
    var backoffUntil = 0;          /* set when the server rate-limits us */
    var lastPersonId = null;       /* suppress immediate re-scan of one person */
    var lastPersonAt = 0;
    /* Set after any resolved automatic outcome. While it is true the kiosk will
     * not offer another automatic entry, however long somebody stands there. It
     * clears only once the scene has read empty for AUTO_DEPARTURE_MS.
     *
     * This is what makes the kiosk a toggle. Gating on elapsed time instead
     * either blocks genuine clocking out (a long interval) or clocks a
     * stationary person in and straight back out (a short one). Gating on
     * absence matches what people actually expect: you are clocked when you
     * arrive, and not clocked again until you have been away. */
    var awaitingDeparture = false;
    var absentSince = 0;

    var RESULT_SECONDS = 6;
    /* Kept in step with the server's interval so the kiosk never starts a
     * countdown the server would then refuse, which would read as it changing
     * its mind. With departure gating doing the real work this is only a
     * backstop, so it is short. */
    var REPEAT_SUPPRESS_MS = (config.minIntervalSeconds || 10) * 1000;
    /* Hands-free capture is deliberately leaner than a button press: two frames
     * instead of three. Capture time dominates how long somebody waits for their
     * name to appear, far more than the recognition itself does. */
    var AUTO_FRAMES = config.autoFrames || 2;
    var FRAME_GAP_MS = config.frameGapMs || 300;
    var REQUIRE_DEPARTURE = config.requireDeparture !== false;
    var DEPARTURE_MS = config.departureMs || 900;

    var missStreak = 0;
    var MISS_HINT_AFTER = 3;

    /* Turn a refusal code into something the person can act on. */
    function missHint(code) {
        if (code === "face_too_small") {
            return "Please come a little closer to the camera";
        }
        if (code === "face_too_blurred") {
            return "Hold still — the image is blurred";
        }
        if (code === "multiple_faces") {
            return "One at a time, please — step up to the camera";
        }
        if (code && code.indexOf("liveness_") === 0) {
            return "Look at the camera — move your head slightly";
        }
        if (code === "not_recognised") {
            return "Not recognised — try the Scan button, or see the office";
        }
        if (code === "no_templates" || code === "models_missing") {
            return "Face recognition is not set up — please see the office";
        }
        return "Face the camera, or use the buttons";
    }

    /* --- Wall clock ----------------------------------------------------- */
    function tickClock() {
        var now = new Date();
        clockEl.textContent = now.toLocaleTimeString("en-GB", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
        });
        dateEl.textContent = now.toLocaleDateString("en-GB", {
            weekday: "long",
            day: "numeric",
            month: "long",
            year: "numeric"
        });
    }

    /* --- Result panel --------------------------------------------------- */
    function setResult(kind, name, action, time, detail) {
        resultBox.className = "mf-kiosk-result" + (kind ? " is-" + kind : "");
        nameEl.textContent = name || "";
        actionEl.innerHTML = action || "";
        timeEl.textContent = time || "";
        detailEl.textContent = detail || "";
    }

    function showIdle() {
        state = STATE.IDLE;
        pending = null;
        cancelBtn.hidden = true;
        stopLooking();
        missStreak = 0;
        setResult(
            "",
            "Ready",
            config.autoMode
                ? "Step up to the camera"
                : "Press <strong>Scan</strong> to clock in or out",
            "",
            ""
        );
        hint.textContent = config.autoMode
            ? "Clocking happens automatically — just look at the camera"
            : "Stand square to the camera and press Scan";
    }

    function showResultFor(seconds) {
        state = STATE.RESULT;
        pending = null;
        cancelBtn.hidden = true;
        /* Nothing more happens automatically until this person has left. */
        if (REQUIRE_DEPARTURE) {
            awaitingDeparture = true;
            absentSince = 0;
        }
        /* Stop polling for a face while the result is on screen. Leaving this
         * timer running was a bug: once the screen returned to idle the stale
         * poll kept calling /identify with nobody in front of the camera, and
         * happily started a fresh countdown - which then committed. */
        stopLooking();
        clearTimer("countdown");
        clearTimer("result");
        resultTimer = window.setTimeout(showIdle, (seconds || RESULT_SECONDS) * 1000);
    }

    function clearTimer(which) {
        if (which === "countdown" && countdownTimer) {
            window.clearInterval(countdownTimer);
            countdownTimer = null;
        }
        if (which === "result" && resultTimer) {
            window.clearTimeout(resultTimer);
            resultTimer = null;
        }
    }

    /* --- On-site counter ------------------------------------------------ */
    function refreshOnsite() {
        window
            .fetch(config.onsiteUrl, {
                headers: { "X-Kiosk-Token": config.token },
                credentials: "same-origin"
            })
            .then(function (response) {
                return response.json();
            })
            .then(function (data) {
                if (data && data.ok) {
                    onsiteEl.textContent =
                        data.count === 1 ? "1 person on site" : data.count + " people on site";
                }
            })
            .catch(function () {
                /* A failed counter refresh must never disturb clocking. */
            });
    }

    /* --- Button-press clocking ------------------------------------------ */
    function setButtonsBusy(isBusy) {
        scanBtn.disabled = isBusy;
        scanIn.disabled = isBusy;
        scanOut.disabled = isBusy;
        scanBtn.textContent = isBusy ? "Scanning…" : "Scan";
    }

    function manualScan(direction) {
        if (busy) {
            return;
        }
        /* A deliberate press wins over anything the automatic path is doing. */
        abandonPending();
        busy = true;
        setButtonsBusy(true);
        setResult("", "Hold still…", "Looking at the camera", "", "");

        capture
            .grabSeries(config.frames, 340)
            .then(function (frames) {
                if (!frames.length) {
                    throw new Error("The camera did not return an image.");
                }
                return window.postJson(
                    config.scanUrl,
                    { frames: frames, direction: direction || null },
                    { "X-Kiosk-Token": config.token }
                );
            })
            .then(handleRecorded)
            .catch(function (error) {
                setResult("error", "Problem", error.message || "Please try again", "", "");
                showResultFor(6);
            })
            .then(function () {
                busy = false;
                setButtonsBusy(false);
            });
    }

    /* --- Shared: render a recorded (or duplicate) entry ------------------ */
    function handleRecorded(data) {
        if (!data.ok) {
            if (data.code === "rate_limited") {
                backoffUntil = Date.now() + 15000;
            }
            setResult("error", "Not recorded", data.message || "Please try again", "", "");
            showResultFor(6);
            return;
        }

        var verb = data.direction === "in" ? "Clocked IN" : "Clocked OUT";
        if (data.recorded === false) {
            setResult(
                "warning",
                data.employee.name,
                "Already " + verb.toLowerCase(),
                data.occurred_at,
                "No new entry recorded."
            );
        } else {
            setResult("success", data.employee.name, verb, data.occurred_at, data.occurred_on);
        }
        if (data.employee) {
            lastPersonId = data.employee.id;
            lastPersonAt = Date.now();
        }
        refreshOnsite();
        showResultFor(RESULT_SECONDS);
    }

    /* --- Hands-free: look for a known face ------------------------------ */
    function lookForFace() {
        if (busy || state === STATE.CONFIRMING || state === STATE.RESULT) {
            return;
        }
        if (Date.now() < backoffUntil) {
            return;
        }
        /* Belt and braces: never run recognition at an empty doorway, whatever
         * state the timers have got themselves into. */
        if (!presence.isPresent()) {
            return;
        }
        busy = true;

        capture
            .grabSeries(AUTO_FRAMES, FRAME_GAP_MS)
            .then(function (frames) {
                if (!frames.length) {
                    return null;
                }
                return window.postJson(
                    config.identifyUrl,
                    { frames: frames },
                    { "X-Kiosk-Token": config.token }
                );
            })
            .then(function (data) {
                if (!data) {
                    return;
                }
                if (!data.ok) {
                    if (data.code === "rate_limited") {
                        /* Stop hammering; the kiosk is polling too fast. */
                        backoffUntil = Date.now() + 15000;
                        return;
                    }
                    /* A single miss is normal while somebody walks up, so the
                     * screen stays quiet rather than flickering. But staying
                     * quiet for ever is worse than a flicker: somebody the
                     * recogniser keeps refusing would be left watching a screen
                     * that appears to be doing nothing. After a few consecutive
                     * misses, say what would help. */
                    missStreak += 1;
                    if (missStreak >= MISS_HINT_AFTER) {
                        hint.textContent = missHint(data.code);
                    }
                    return;
                }
                missStreak = 0;

                if (data.code === "already_clocked") {
                    /* Only tell them if they are actually standing there, not
                     * every time they cross the camera's view. */
                    setResult(
                        "warning",
                        data.employee.name,
                        data.message,
                        data.occurred_at,
                        "Use the buttons if you need to clock again."
                    );
                    lastPersonId = data.employee.id;
                    lastPersonAt = Date.now();
                    showResultFor(4);
                    return;
                }

                if (data.code === "pending" && data.confirm_token) {
                    if (
                        data.employee.id === lastPersonId &&
                        Date.now() - lastPersonAt < REPEAT_SUPPRESS_MS
                    ) {
                        return; /* just dealt with this person */
                    }
                    beginCountdown(data);
                }
            })
            .catch(function () {
                /* Network hiccup: try again on the next tick. */
            })
            .then(function () {
                busy = false;
            });
    }

    /* --- Hands-free: the cancellable countdown -------------------------- */
    function beginCountdown(data) {
        state = STATE.CONFIRMING;
        /* One countdown at a time: stop looking for faces until it resolves. */
        stopLooking();
        pending = {
            token: data.confirm_token,
            employee: data.employee,
            direction: data.direction
        };

        var remaining = typeof data.confirm_seconds === "number" ? data.confirm_seconds : 4;
        var verb = data.direction === "in" ? "Clocking IN" : "Clocking OUT";
        cancelBtn.hidden = false;

        function paint() {
            setResult(
                data.direction === "in" ? "success" : "warning",
                data.employee.name,
                verb,
                remaining > 0 ? String(remaining) : "",
                remaining > 0 ? "Press Cancel if this is not right" : "Recording…"
            );
        }

        if (remaining <= 0) {
            paint();
            commitPending();
            return;
        }

        paint();
        clearTimer("countdown");
        countdownTimer = window.setInterval(function () {
            remaining -= 1;
            if (remaining <= 0) {
                clearTimer("countdown");
                paint();
                commitPending();
                return;
            }
            paint();
        }, 1000);
    }

    function commitPending() {
        if (!pending || busy) {
            return;
        }
        var token = pending.token;
        busy = true;

        window
            .postJson(
                config.commitUrl,
                { confirm_token: token },
                { "X-Kiosk-Token": config.token }
            )
            .then(handleRecorded)
            .catch(function (error) {
                setResult("error", "Problem", error.message || "Please try again", "", "");
                showResultFor(6);
            })
            .then(function () {
                busy = false;
            });
    }

    function abandonPending() {
        clearTimer("countdown");
        pending = null;
        cancelBtn.hidden = true;
    }

    function cancelPending() {
        if (state !== STATE.CONFIRMING) {
            return;
        }
        var name = pending && pending.employee ? pending.employee.first_name : "";
        abandonPending();
        setResult("", "Cancelled", name ? "Nothing recorded, " + name + "." : "Nothing recorded.", "", "");
        /* Suppress this person briefly so the countdown does not restart at once. */
        showResultFor(3);
    }

    /* --- Hands-free: the presence loop ---------------------------------- */
    function watchForArrivals() {
        /* Measure every tick, even mid-countdown: that is how a departure gets
         * noticed promptly rather than only once the screen returns to idle. */
        var score = presence.measure();
        var somebodyThere = score >= config.presenceThreshold;

        if (!somebodyThere) {
            if (!absentSince) {
                absentSince = Date.now();
            } else if (awaitingDeparture && Date.now() - absentSince >= DEPARTURE_MS) {
                /* They have gone. The next arrival is a fresh clocking, which is
                 * what turns "clocked in" into "clocked out" next time. */
                awaitingDeparture = false;
                lastPersonId = null;
            }
        } else {
            absentSince = 0;
        }

        if (state !== STATE.IDLE && state !== STATE.LOOKING) {
            return;
        }

        if (awaitingDeparture) {
            if (somebodyThere) {
                hint.textContent = "All set — step away from the camera";
            }
            return;
        }

        if (somebodyThere && state === STATE.IDLE) {
            state = STATE.LOOKING;
            setResult("", "Hold still…", "Checking who you are", "", "");
            hint.textContent = "Look at the camera";
            lookForFace();
            if (!lookTimer) {
                lookTimer = window.setInterval(lookForFace, config.pollMs);
            }
        } else if (!somebodyThere && state === STATE.LOOKING) {
            /* They left before being recognised. */
            state = STATE.IDLE;
            stopLooking();
            showIdle();
        }
    }

    function stopLooking() {
        if (lookTimer) {
            window.clearInterval(lookTimer);
            lookTimer = null;
        }
    }

    /* --- Start up ------------------------------------------------------- */
    tickClock();
    window.setInterval(tickClock, 1000);

    scanBtn.addEventListener("click", function () {
        manualScan(null);
    });
    scanIn.addEventListener("click", function () {
        manualScan("in");
    });
    scanOut.addEventListener("click", function () {
        manualScan("out");
    });
    cancelBtn.addEventListener("click", cancelPending);

    /* Space or Enter triggers a scan, so a cheap USB footswitch wired as a
     * keyboard works as the trigger. Escape cancels a pending automatic entry. */
    document.addEventListener("keydown", function (event) {
        if (event.code === "Escape") {
            event.preventDefault();
            cancelPending();
            return;
        }
        if (event.code === "Space" || event.code === "Enter" || event.code === "NumpadEnter") {
            event.preventDefault();
            manualScan(null);
        }
    });

    capture
        .start()
        .then(function () {
            scanBtn.disabled = false;
            showIdle();
            refreshOnsite();
            window.setInterval(refreshOnsite, 60000);

            if (config.autoMode) {
                modeEl.textContent = "Automatic";
                modeEl.className = "mf-badge mf-badge-in";
                /* Let the detector learn the empty scene before watching. */
                window.setTimeout(function () {
                    presence.reset();
                    presenceTimer = window.setInterval(watchForArrivals, config.presenceMs);
                }, 1200);
            } else {
                modeEl.textContent = "Press to scan";
                modeEl.className = "mf-badge";
            }
        })
        .catch(function (error) {
            hint.textContent = error.message;
            setResult("error", "Camera unavailable", error.message, "", "");
        });
})();
