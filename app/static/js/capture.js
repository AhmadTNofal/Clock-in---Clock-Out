/* Shared webcam capture helper.
 *
 * Used by the kiosk, the enrolment page and the camera check. Frames are
 * downscaled and JPEG-compressed in the browser before upload: the server
 * downscales to 640px anyway, so sending full-resolution frames would only
 * waste bandwidth and time.
 *
 * Note on browsers: getUserMedia is only available on a secure origin. That
 * means https, or http on localhost. A kiosk reaching the server by LAN IP over
 * plain http will be refused camera access by the browser - see the README.
 */
(function (global) {
    "use strict";

    var MAX_WIDTH = 640;
    var JPEG_QUALITY = 0.82;

    function FaceCapture(videoEl, options) {
        this.video = videoEl;
        this.options = options || {};
        this.stream = null;
        this.canvas = document.createElement("canvas");
    }

    FaceCapture.prototype.isSupported = function () {
        return !!(global.navigator.mediaDevices && global.navigator.mediaDevices.getUserMedia);
    };

    FaceCapture.prototype.start = function () {
        var self = this;
        if (!this.isSupported()) {
            return Promise.reject(
                new Error(
                    "This browser will not allow camera access. Use Chrome or Edge, " +
                        "and open the page over https or on localhost."
                )
            );
        }
        return global.navigator.mediaDevices
            .getUserMedia({
                video: {
                    width: { ideal: 1280 },
                    height: { ideal: 720 },
                    facingMode: "user"
                },
                audio: false
            })
            .then(function (stream) {
                self.stream = stream;
                self.video.srcObject = stream;
                return self.video.play();
            })
            .then(function () {
                return self.waitForFrame();
            });
    };

    /* Wait until the video actually has pixel dimensions. Reading a frame
     * before this point yields a blank image on some webcams. */
    FaceCapture.prototype.waitForFrame = function () {
        var video = this.video;
        return new Promise(function (resolve) {
            if (video.videoWidth > 0) {
                resolve();
                return;
            }
            var tries = 0;
            var timer = global.setInterval(function () {
                tries += 1;
                if (video.videoWidth > 0 || tries > 60) {
                    global.clearInterval(timer);
                    resolve();
                }
            }, 50);
        });
    };

    FaceCapture.prototype.stop = function () {
        if (this.stream) {
            this.stream.getTracks().forEach(function (track) {
                track.stop();
            });
            this.stream = null;
        }
    };

    /* Grab one frame as a JPEG data URL. */
    FaceCapture.prototype.grab = function () {
        var video = this.video;
        if (!video.videoWidth) {
            return null;
        }
        var scale = Math.min(1, MAX_WIDTH / video.videoWidth);
        var width = Math.round(video.videoWidth * scale);
        var height = Math.round(video.videoHeight * scale);
        this.canvas.width = width;
        this.canvas.height = height;
        var ctx = this.canvas.getContext("2d");
        /* Drawn unmirrored: the preview is flipped by CSS for the user's
         * benefit, but the recogniser should see the real orientation. */
        ctx.drawImage(video, 0, 0, width, height);
        return this.canvas.toDataURL("image/jpeg", JPEG_QUALITY);
    };

    /* Grab *count* frames spaced *gapMs* apart.
     *
     * The gap matters: consecutive frames grabbed in the same tick are nearly
     * identical, which the server's liveness check would read as a photo. About
     * a third of a second apart captures natural movement. */
    FaceCapture.prototype.grabSeries = function (count, gapMs) {
        var self = this;
        var frames = [];
        var gap = gapMs || 320;

        function next(remaining) {
            var frame = self.grab();
            if (frame) {
                frames.push(frame);
            }
            if (remaining <= 1) {
                return Promise.resolve(frames);
            }
            return new Promise(function (resolve) {
                global.setTimeout(resolve, gap);
            }).then(function () {
                return next(remaining - 1);
            });
        }

        return next(count);
    };

    /* --- Presence detection -------------------------------------------------
     *
     * Hands-free mode must not run face recognition flat out all day: that is
     * 38 MB of model inference per frame, for an empty doorway. So the browser
     * answers the cheap question first - "has somebody arrived?" - and only then
     * asks the server the expensive one, "who is it?".
     *
     * The measure is the mean grey-level difference between a small greyscale
     * frame and a reference image of the empty scene. Comparing against a
     * *reference* rather than the previous frame matters: somebody standing
     * still produces almost no frame-to-frame change but a large difference
     * from the empty doorway, and it is exactly the person standing still,
     * waiting to be clocked, that we must not miss.
     *
     * The reference is re-learned whenever the scene reads as empty, so the
     * daylight changing through the workshop windows does not slowly turn into
     * a permanent false positive.
     */
    function PresenceDetector(capture, options) {
        options = options || {};
        this.capture = capture;
        this.threshold = options.threshold || 7.0;
        this.width = 64;
        this.height = 48;
        this.reference = null;
        this.canvas = document.createElement("canvas");
        this.canvas.width = this.width;
        this.canvas.height = this.height;
    }

    PresenceDetector.prototype._grey = function () {
        var video = this.capture.video;
        if (!video.videoWidth) {
            return null;
        }
        var ctx = this.canvas.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(video, 0, 0, this.width, this.height);
        var data = ctx.getImageData(0, 0, this.width, this.height).data;
        var grey = new Float32Array(this.width * this.height);
        for (var i = 0, p = 0; i < data.length; i += 4, p += 1) {
            /* Rec. 601 luma - matches how OpenCV converts to grey. */
            grey[p] = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
        }
        return grey;
    };

    /* Returns the mean absolute difference from the empty-scene reference. */
    PresenceDetector.prototype.measure = function () {
        var grey = this._grey();
        if (!grey) {
            return 0;
        }
        if (!this.reference) {
            this.reference = grey;
            return 0;
        }
        var total = 0;
        for (var i = 0; i < grey.length; i += 1) {
            total += Math.abs(grey[i] - this.reference[i]);
        }
        var score = total / grey.length;

        if (score < this.threshold) {
            /* Scene reads as empty: drift the reference towards it so gradual
             * lighting changes are absorbed rather than accumulating. */
            for (var j = 0; j < grey.length; j += 1) {
                this.reference[j] = this.reference[j] * 0.9 + grey[j] * 0.1;
            }
        }
        return score;
    };

    PresenceDetector.prototype.isPresent = function () {
        return this.measure() >= this.threshold;
    };

    /* Forget the learned background - used after a result is shown, so the next
     * person is measured against the empty scene rather than their predecessor. */
    PresenceDetector.prototype.reset = function () {
        this.reference = null;
    };

    function postJson(url, body, extraHeaders) {
        var headers = { "Content-Type": "application/json" };
        Object.keys(extraHeaders || {}).forEach(function (key) {
            headers[key] = extraHeaders[key];
        });
        return global
            .fetch(url, {
                method: "POST",
                headers: headers,
                body: JSON.stringify(body),
                credentials: "same-origin"
            })
            .then(function (response) {
                return response.json().catch(function () {
                    return {
                        ok: false,
                        code: "bad_response",
                        message: "The server returned an unreadable response."
                    };
                });
            });
    }

    global.FaceCapture = FaceCapture;
    global.PresenceDetector = PresenceDetector;
    global.postJson = postJson;
})(window);
