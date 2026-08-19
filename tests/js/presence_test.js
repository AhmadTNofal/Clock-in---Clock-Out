/* Tests PresenceDetector against the awkward ways people actually leave.
 *
 * The detector holds a reference image of the empty scene and calls "somebody is
 * there" when the current frame differs from it. The danger is what it learns:
 * if it adapts towards any frame that happens to read below the threshold, a
 * person lingering at the edge of view gets absorbed into the reference. After
 * that, they are invisible when they come back - which shows up as clocking that
 * works on the way in and is unreliable ever after.
 */
const fs = require("fs");
const path = require("path");

const APP = path.resolve(__dirname, "..", "..", "app", "static", "js");

const W = 64;
const H = 48;

/* A scene is described by a single grey level for the background plus an
 * optional "person" occupying a fraction of the frame at a brighter level. */
let scene = { bg: 100, personFraction: 0, personLevel: 220, noise: 0 };
let rng = 1;
function rand() {
  rng = (rng * 1103515245 + 12345) & 0x7fffffff;
  return rng / 0x7fffffff;
}

function framePixels() {
  const data = new Uint8ClampedArray(W * H * 4);
  const personCols = Math.round(W * scene.personFraction);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 4;
      let v = x < personCols ? scene.personLevel : scene.bg;
      if (scene.noise) v += (rand() - 0.5) * 2 * scene.noise;
      v = Math.max(0, Math.min(255, v));
      data[i] = data[i + 1] = data[i + 2] = v;
      data[i + 3] = 255;
    }
  }
  return data;
}

global.document = {
  createElement: () => ({
    width: 0,
    height: 0,
    getContext: () => ({
      drawImage() {},
      getImageData: () => ({ data: framePixels() }),
    }),
  }),
};
Object.defineProperty(globalThis, "navigator", {
  value: { mediaDevices: { getUserMedia: () => Promise.resolve({}) } },
  writable: true,
  configurable: true,
});
global.window = global;
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = (fn) => { fn(); return 0; };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });

eval(fs.readFileSync(path.join(APP, "capture.js"), "utf8"));

const THRESHOLD = 7.0;
const fakeCapture = { video: { videoWidth: 640, videoHeight: 480 } };

function newDetector() {
  return new global.PresenceDetector(fakeCapture, { threshold: THRESHOLD });
}

/* Run the detector for a number of ticks and return the final score. */
function run(det, ticks) {
  let score = 0;
  for (let i = 0; i < ticks; i++) score = det.measure();
  return score;
}

const results = [];
function check(label, ok, extra) {
  results.push({ label, ok: !!ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${extra ? "   [" + extra + "]" : ""}`);
}

// --------------------------------------------------------------------------
console.log("--- baseline behaviour ---");
{
  const det = newDetector();
  scene = { bg: 100, personFraction: 0, personLevel: 220, noise: 1 };
  run(det, 20);                                   // learn the empty scene
  const empty = det.measure();
  check("an empty scene reads as empty", empty < THRESHOLD, `score=${empty.toFixed(2)}`);

  scene.personFraction = 0.5;                     // somebody steps in
  const present = det.measure();
  check("a person in view reads as present", present >= THRESHOLD,
        `score=${present.toFixed(2)}`);
}

console.log("\n--- the reported failure: works on the way in, then unreliable ---");
{
  const det = newDetector();
  scene = { bg: 100, personFraction: 0, personLevel: 220, noise: 1 };
  run(det, 20);                                   // empty scene learnt

  scene.personFraction = 0.5;                     // arrives, gets clocked
  check("arrival detected", det.measure() >= THRESHOLD);

  /* Now they drift to the edge of view on their way out - still partly visible,
   * but faint enough that the raw difference sits below the threshold. This is
   * the frame that must NOT be learnt as "empty". */
  scene.personFraction = 0.03;
  run(det, 25);                                   // linger there a few seconds

  scene.personFraction = 0;                       // fully gone
  run(det, 10);

  scene.personFraction = 0.5;                     // and they come back
  const onReturn = det.measure();
  check("RETURN AFTER LINGERING IS DETECTED", onReturn >= THRESHOLD,
        `score=${onReturn.toFixed(2)} threshold=${THRESHOLD}`);
}

console.log("\n--- a person who stands still must not fade away ---");
{
  const det = newDetector();
  scene = { bg: 100, personFraction: 0, personLevel: 220, noise: 1 };
  run(det, 20);
  scene.personFraction = 0.5;
  run(det, 150);                                  // stands there ~30s
  const stillThere = det.measure();
  check("still detected after standing 30s", stillThere >= THRESHOLD,
        `score=${stillThere.toFixed(2)}`);
}

console.log("\n--- gradual lighting change must not become a false trigger ---");
{
  const det = newDetector();
  scene = { bg: 100, personFraction: 0, personLevel: 220, noise: 1 };
  run(det, 20);
  for (let i = 0; i < 200; i++) {                 // daylight drifts up 40 levels
    scene.bg = 100 + (40 * i) / 200;
    det.measure();
  }
  const drifted = det.measure();
  check("slow lighting drift still reads as empty", drifted < THRESHOLD,
        `score=${drifted.toFixed(2)}`);
  scene.personFraction = 0.5;
  const afterDrift = det.measure();
  check("a person is still detected after the drift", afterDrift >= THRESHOLD,
        `score=${afterDrift.toFixed(2)}`);
}

console.log("\n--- a brief pass-through must not be learnt ---");
{
  const det = newDetector();
  scene = { bg: 100, personFraction: 0, personLevel: 220, noise: 1 };
  run(det, 20);
  scene.personFraction = 0.5;
  run(det, 3);                                    // someone crosses quickly
  scene.personFraction = 0;
  run(det, 10);
  scene.personFraction = 0.5;
  const later = det.measure();
  check("detection still works after a pass-through", later >= THRESHOLD,
        `score=${later.toFixed(2)}`);
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) console.log("failed: " + failed.map((f) => f.label).join("; "));
process.exit(failed.length ? 1 : 0);
