/* Enrolment page: capture several samples, review them, then save. */
(function () {
    "use strict";

    var config = window.ENROL_CONFIG;
    var video = document.getElementById("enrol-video");
    var hint = document.getElementById("enrol-hint");
    var captureBtn = document.getElementById("capture-btn");
    var clearBtn = document.getElementById("clear-btn");
    var saveBtn = document.getElementById("save-btn");
    var thumbs = document.getElementById("thumbs");
    var status = document.getElementById("enrol-status");
    var replaceBox = document.getElementById("replace-existing");

    var capture = new window.FaceCapture(video);
    var frames = [];

    function setStatus(kind, message) {
        status.innerHTML = message
            ? '<div class="mf-flash mf-flash-' + kind + '">' + message + "</div>"
            : "";
    }

    function render() {
        thumbs.innerHTML = "";
        frames.forEach(function (frame, position) {
            var img = document.createElement("img");
            img.src = frame;
            img.alt = "Sample " + (position + 1);
            img.title = "Click to remove sample " + (position + 1);
            img.addEventListener("click", function () {
                frames.splice(position, 1);
                render();
            });
            thumbs.appendChild(img);
        });

        saveBtn.disabled = frames.length < config.minSamples;
        captureBtn.disabled = !capture.stream || frames.length >= config.maxSamples;
        hint.textContent =
            frames.length + " of up to " + config.maxSamples + " sample(s) captured" +
            (frames.length < config.minSamples
                ? " — at least " + config.minSamples + " needed."
                : " — ready to save.");
    }

    captureBtn.addEventListener("click", function () {
        var frame = capture.grab();
        if (!frame) {
            setStatus("error", "The camera did not return an image.");
            return;
        }
        frames.push(frame);
        setStatus("", "");
        render();
    });

    clearBtn.addEventListener("click", function () {
        frames = [];
        setStatus("", "");
        render();
    });

    saveBtn.addEventListener("click", function () {
        saveBtn.disabled = true;
        setStatus("warning", "Checking samples…");

        window
            .postJson(
                config.submitUrl,
                { frames: frames, replace: !!replaceBox.checked },
                { "X-CSRFToken": config.csrfToken }
            )
            .then(function (data) {
                if (data.ok) {
                    setStatus(
                        "success",
                        data.message + " Redirecting to the employee record…"
                    );
                    window.setTimeout(function () {
                        window.location.href = config.detailUrl;
                    }, 1400);
                    return;
                }
                var message = data.message || "Enrolment failed.";
                if (data.rejected && data.rejected.length) {
                    message += "<br>" + data.rejected.join("<br>");
                }
                setStatus("error", message);
                saveBtn.disabled = false;
            })
            .catch(function (error) {
                setStatus("error", error.message || "Enrolment failed.");
                saveBtn.disabled = false;
            });
    });

    capture
        .start()
        .then(function () {
            render();
        })
        .catch(function (error) {
            hint.textContent = error.message;
            setStatus("error", error.message);
        });
})();
