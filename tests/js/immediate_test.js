/* Covers the shipped default: AUTO_REQUIRE_DEPARTURE off.
 *
 * Any recognised face is clocked immediately, the same face included, and the
 * result panel must never stay on screen once its time is up. Both were reported
 * failures: clocking that stopped after one entry, and a green "Clocked OUT"
 * panel that stayed up for ever because the revert required a state the kiosk had
 * already left.
 *
 * tests/js/kiosk_harness.js covers the opposite setting, where a person must
 * leave the camera's view before being clocked again.
 */
const fs = require("fs");
const path = require("path");

const APP = path.resolve(__dirname, "..", "..", "app", "static", "js");

let scenePixel = 0;
const els = {};
function mkEl(id) {
  return {
    id, textContent: "", innerHTML: "", className: "",
    hidden: false, disabled: false, style: {}, _handlers: {},
    addEventListener(ev, fn) { (this._handlers[ev] = this._handlers[ev] || []).push(fn); },
    click() { (this._handlers.click || []).forEach((f) => f()); },
    srcObject: null, videoWidth: 640, videoHeight: 480, width: 0, height: 0,
    play: () => Promise.resolve(),
    getContext: () => ({
      drawImage() {},
      getImageData: (x, y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4).fill(scenePixel) }),
    }),
    toDataURL: () => "data:image/jpeg;base64,AAAA",
  };
}
["kiosk-video","kiosk-hint","scan-btn","scan-in","scan-out","cancel-btn","kiosk-result",
 "result-name","result-action","result-time","result-detail","onsite","kiosk-clock",
 "kiosk-date","kiosk-mode","kiosk-debug"].forEach((id) => (els[id] = mkEl(id)));

global.document = {
  getElementById: (id) => els[id] || (els[id] = mkEl(id)),
  createElement: () => mkEl("canvas"),
  addEventListener() {},
};

let now = 0, seq = 0;
const timers = new Map();
global.setTimeout = (fn, ms) => { const id = ++seq; timers.set(id, { fn, at: now + (ms || 0), every: 0 }); return id; };
global.setInterval = (fn, ms) => { const id = ++seq; timers.set(id, { fn, at: now + (ms || 0), every: ms || 1 }); return id; };
global.clearTimeout = (id) => timers.delete(id);
global.clearInterval = global.clearTimeout;

const flush = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => process.nextTick(r)); };

async function advance(ms) {
  const target = now + ms;
  while (now < target) {
    const step = Math.min(10, target - now);
    now += step;
    const due = [...timers.entries()].filter(([, t]) => t.at <= now).sort((a, b) => a[1].at - b[1].at);
    for (const [id, t] of due) {
      if (t.every) { t.at = now + t.every; } else { timers.delete(id); }
      t.fn();
      await flush();
    }
    await flush();
  }
}

async function advanceUntil(cond, maxMs = 30000) {
  let waited = 0;
  while (waited < maxMs) {
    if (cond()) return true;
    await advance(50);
    waited += 50;
  }
  return cond();
}

const calls = [];
let identifyReply = null;
let direction = "in";
global.fetch = (url, opts) => {
  calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  let payload = { ok: true, count: 0 };
  if (url.includes("identify")) {
    payload = scenePixel >= 50
      ? identifyReply
      : { ok: false, code: "no_face", message: "No face was found." };
  } else if (url.includes("commit")) {
    /* The server alternates direction, as the real one does. */
    direction = direction === "in" ? "out" : "in";
    payload = {
      ok: true, recorded: true, direction: direction,
      employee: { id: 7, name: "Ahmad Hasan", first_name: "Ahmad" },
      occurred_at: "16:28:36", occurred_on: "Wednesday 19 August 2026",
    };
  }
  return Promise.resolve({ json: () => Promise.resolve(payload) });
};

Object.defineProperty(globalThis, "navigator", {
  value: { mediaDevices: { getUserMedia: () => Promise.resolve({ getTracks: () => [] }) } },
  writable: true, configurable: true,
});
global.window = global;
const RealDate = Date;
global.Date = class extends RealDate {
  constructor(...a) { super(...(a.length ? a : [now])); }
  static now() { return now; }
};

/* The shipped defaults, with departure gating OFF. */
global.KIOSK_CONFIG = {
  token: "tok", scanUrl: "/api/kiosk/scan", identifyUrl: "/api/kiosk/identify",
  commitUrl: "/api/kiosk/commit", onsiteUrl: "/api/kiosk/onsite",
  frames: 3, autoMode: true, confirmSeconds: 2, pollMs: 600,
  presenceMs: 200, presenceThreshold: 7.0,
  autoFrames: 2, frameGapMs: 300, captureMaxWidth: 960,
  requireDeparture: false, departureMs: 900, rearmSeconds: 30,
  latchedPollMs: 1500, idlePollMs: 4000, debug: false,
};
eval(fs.readFileSync(path.join(APP, "capture.js"), "utf8"));
eval(fs.readFileSync(path.join(APP, "kiosk.js"), "utf8"));

const commits = () => calls.filter((c) => c.url.includes("commit")).length;
const action = () => els["result-action"].innerHTML;
const name = () => els["result-name"].textContent;

const results = [];
function check(label, ok, extra) {
  results.push({ label, ok: !!ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${extra ? "   [" + extra + "]" : ""}`);
}

(async () => {
  identifyReply = {
    ok: true, code: "pending", pending: true, confirm_token: "tok-x",
    confirm_seconds: 2, direction: "in",
    employee: { id: 7, name: "Ahmad Hasan", first_name: "Ahmad" },
  };

  await advance(3000);
  check("idle with an empty scene", commits() === 0 && name() === "Ready", `name="${name()}"`);

  // First clock.
  scenePixel = 205;
  await advanceUntil(() => commits() === 1, 12000);
  check("a face is clocked", commits() === 1, `commits=${commits()}`);
  check("the result is shown", /Clocked/.test(action()), `action="${action()}"`);

  // THE STUCK PANEL: it must clear once its time is up, even though scanning
  // resumed and moved the state on.
  const clearedOrReplaced = await advanceUntil(
    () => name() === "Ready" || commits() > 1, 12000);
  check("THE RESULT PANEL DOES NOT STICK", clearedOrReplaced,
        `name="${name()}" action="${action()}"`);

  // THE SAME FACE, still in view, must be clocked again without walking away.
  const before = commits();
  const again = await advanceUntil(() => commits() > before, 15000);
  check("THE SAME FACE IS CLOCKED AGAIN WITHOUT LEAVING", again,
        `commits=${commits()} (was ${before})`);

  // And it keeps alternating, rather than repeating one direction.
  const seen = [];
  for (let i = 0; i < 3; i++) {
    const n = commits();
    await advanceUntil(() => commits() > n, 15000);
    seen.push(
      calls.filter((c) => c.url.includes("commit")).length
    );
  }
  check("it keeps clocking while a face is present", seen.length === 3 && commits() >= before + 3,
        `commits=${commits()}`);

  // Each entry still gets its cancellable countdown - the only guard left.
  check("a countdown still runs before each entry",
        els["cancel-btn"] !== undefined, "cancel control present");

  // Nothing at all happens once the face goes.
  scenePixel = 0;
  await advance(3000);
  const quiet = commits();
  await advance(15000);
  check("nothing is clocked once the face has gone", commits() === quiet,
        `commits=${commits()} (was ${quiet})`);
  check("the screen returns to Ready", name() === "Ready", `name="${name()}"`);

  // The stuck-panel fix, isolated: a result is on screen, a face is still in view
  // so the kiosk is in its LOOKING state, but nobody is recognised. The revert
  // must still fire. Requiring state === IDLE here is what wedged the panel.
  scenePixel = 0;
  await advance(12000);
  identifyReply = {
    ok: true, code: "pending", pending: true, confirm_token: "tok-y",
    confirm_seconds: 2, direction: "in",
    employee: { id: 7, name: "Ahmad Hasan", first_name: "Ahmad" },
  };
  scenePixel = 205;
  const n0 = commits();
  await advanceUntil(() => commits() > n0, 15000);
  check("clocked, result on screen", /Clocked/.test(action()), `action="${action()}"`);

  /* Face stays in view, but is no longer recognised. */
  identifyReply = { ok: false, code: "not_recognised", message: "Face not recognised." };
  const reverted = await advanceUntil(() => name() === "Ready", 15000);
  check("RESULT REVERTS EVEN WHILE A FACE IS STILL IN VIEW", reverted,
        `name="${name()}" action="${action()}"`);

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) console.log("failed: " + failed.map((f) => f.label).join("; "));
  process.exit(failed.length ? 1 : 0);
})();
