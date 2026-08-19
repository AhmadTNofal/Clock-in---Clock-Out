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
    global.postJson = postJson;
})(window);
