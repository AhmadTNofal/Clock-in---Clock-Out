/* Kiosk screen behaviour: clock, camera, scan, result display. */
(function () {
    "use strict";

    var config = window.KIOSK_CONFIG;
    var video = document.getElementById("kiosk-video");
    var hint = document.getElementById("kiosk-hint");
    var scanBtn = document.getElementById("scan-btn");
    var scanIn = document.getElementById("scan-in");
    var scanOut = document.getElementById("scan-out");
    var resultBox = document.getElementById("kiosk-result");
    var nameEl = document.getElementById("result-name");
    var actionEl = document.getElementById("result-action");
    var timeEl = document.getElementById("result-time");
    var detailEl = document.getElementById("result-detail");
    var onsiteEl = document.getElementById("onsite");
    var clockEl = document.getElementById("kiosk-clock");
    var dateEl = document.getElementById("kiosk-date");

    var capture = new window.FaceCapture(video);
    var busy = false;
    var resetTimer = null;

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
    function setResult(state, name, action, time, detail) {
        resultBox.className = "mf-kiosk-result" + (state ? " is-" + state : "");
        nameEl.textContent = name || "";
        actionEl.innerHTML = action || "";
        timeEl.textContent = time || "";
        detailEl.textContent = detail || "";
    }

    function resetSoon(seconds) {
        if (resetTimer) {
            window.clearTimeout(resetTimer);
        }
        resetTimer = window.setTimeout(function () {
            setResult("", "Ready", "Press <strong>Scan</strong> to clock in or out", "", "");
        }, (seconds || 6) * 1000);
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
                        data.count === 1
                            ? "1 person on site"
                            : data.count + " people on site";
                }
            })
            .catch(function () {
                /* A failed counter refresh must never disturb clocking. */
            });
    }

    /* --- Scanning ------------------------------------------------------- */
    function setBusy(state) {
        busy = state;
        scanBtn.disabled = state;
        scanIn.disabled = state;
        scanOut.disabled = state;
        scanBtn.textContent = state ? "Scanning…" : "Scan";
    }

    function doScan(direction) {
        if (busy) {
            return;
        }
        setBusy(true);
        setResult("", "Hold still…", "Looking at the camera", "", "");
        hint.textContent = "Keep your face in the frame";

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
            .then(function (data) {
                if (data.ok) {
                    var verb = data.direction === "in" ? "Clocked IN" : "Clocked OUT";
                    if (!data.recorded) {
                        setResult(
                            "warning",
                            data.employee.name,
                            "Already " + verb.toLowerCase(),
                            data.occurred_at,
                            "No new entry recorded — you scanned a moment ago."
                        );
                    } else {
                        setResult(
                            "success",
                            data.employee.name,
                            verb,
                            data.occurred_at,
                            data.occurred_on
                        );
                    }
                    refreshOnsite();
                } else {
                    setResult("error", "Not recorded", data.message || "Please try again", "", "");
                }
                hint.textContent = "Stand square to the camera and press Scan";
                resetSoon(7);
            })
            .catch(function (error) {
                setResult("error", "Problem", error.message || "Please try again", "", "");
                resetSoon(7);
            })
            .then(function () {
                setBusy(false);
            });
    }

    /* --- Start up ------------------------------------------------------- */
    tickClock();
    window.setInterval(tickClock, 1000);

    scanBtn.addEventListener("click", function () {
        doScan(null);
    });
    scanIn.addEventListener("click", function () {
        doScan("in");
    });
    scanOut.addEventListener("click", function () {
        doScan("out");
    });

    /* Space or Enter triggers a scan, so a cheap USB footswitch or barcode-style
     * button wired as a keyboard works as the scan trigger. */
    document.addEventListener("keydown", function (event) {
        if (event.code === "Space" || event.code === "Enter" || event.code === "NumpadEnter") {
            event.preventDefault();
            doScan(null);
        }
    });

    capture
        .start()
        .then(function () {
            hint.textContent = "Stand square to the camera and press Scan";
            scanBtn.disabled = false;
            refreshOnsite();
            window.setInterval(refreshOnsite, 60000);
        })
        .catch(function (error) {
            hint.textContent = error.message;
            setResult("error", "Camera unavailable", error.message, "", "");
        });
})();
