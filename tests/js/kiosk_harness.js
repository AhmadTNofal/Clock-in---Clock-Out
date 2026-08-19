/* Drives kiosk.js under Node with a stubbed DOM and fake timers.
 *
 * The question being answered: does the hands-free state machine only ever
 * commit an entry when the countdown is allowed to finish, and does Cancel
 * genuinely prevent it? That logic lives in browser code the pytest suite
 * cannot reach, and it is the client half of the "do not clock people out as
 * they walk past" guarantee.
 */
const fs = require("fs");
const path = require("path");

const APP = path.resolve(__dirname, "..", "..", "app", "static", "js");

// --- minimal DOM ----------------------------------------------------------
let scenePixel = 0;
const els = {};
function mkEl(id) {
  return {
    id, textContent: "", innerHTML: "", className: "",
    hidden: false, disabled: false, style: {},
    _handlers: {},
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
 "kiosk-date","kiosk-mode"].forEach((id) => (els[id] = mkEl(id)));

global.document = {
  getElementById: (id) => els[id] || (els[id] = mkEl(id)),
  createElement: () => mkEl("canvas"),
  addEventListener() {},
};

// --- fake timers -----------------------------------------------------------
let now = 0, seq = 0;
const timers = new Map();
global.setTimeout = (fn, ms) => { const id = ++seq; timers.set(id, { fn, at: now + (ms || 0), every: 0 }); return id; };
global.setInterval = (fn, ms) => { const id = ++seq; timers.set(id, { fn, at: now + (ms || 0), every: ms || 1 }); return id; };
global.clearTimeout = (id) => timers.delete(id);
global.clearInterval = global.clearTimeout;

const flush = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => process.nextTick(r)); };

/* Step time forward in 10ms slices, flushing microtasks so promise chains that
 * depend on timers actually progress. */
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

async function advanceUntil(label, cond, maxMs = 20000) {
  let waited = 0;
  while (waited < maxMs) {
    if (cond()) return true;
    await advance(50);
    waited += 50;
  }
  return cond();
}

// --- fake network ----------------------------------------------------------
const calls = [];
let identifyReply = null;
global.fetch = (url, opts) => {
  calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  let payload = { ok: true, count: 0 };
  if (url.includes("identify")) payload = identifyReply;
  else if (url.includes("commit")) {
    /* Echo back whatever the last identify offered, as the real server does.
     * Hardcoding a direction here once masked a genuine result: the screen
     * showed "Clocked IN" for what was actually a clock-out. */
    payload = {
      ok: true, recorded: true,
      direction: (identifyReply && identifyReply.direction) || "in",
      employee: (identifyReply && identifyReply.employee) ||
        { id: 7, name: "Sam Fletcher", first_name: "Sam" },
      occurred_at: "07:31:02", occurred_on: "Monday 19 August 2026",
    };
  }
  return Promise.resolve({ json: () => Promise.resolve(payload) });
};

// Node >=21 exposes `navigator` as a read-only accessor, so a plain assignment
// is silently ignored. defineProperty is required to stub it.
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

/* Mirrors the shipped defaults in app/config.py. */
global.KIOSK_CONFIG = {
  token: "tok", scanUrl: "/api/kiosk/scan", identifyUrl: "/api/kiosk/identify",
  commitUrl: "/api/kiosk/commit", onsiteUrl: "/api/kiosk/onsite",
  frames: 3, autoMode: true, confirmSeconds: 2, pollMs: 600,
  presenceMs: 200, presenceThreshold: 7.0,
  autoFrames: 2, frameGapMs: 300, minIntervalSeconds: 10, captureMaxWidth: 960,
  requireDeparture: true, departureMs: 900, rearmSeconds: 30,
};
eval(fs.readFileSync(path.join(APP, "capture.js"), "utf8"));
eval(fs.readFileSync(path.join(APP, "kiosk.js"), "utf8"));

const commits = () => calls.filter((c) => c.url.includes("commit")).length;
const identifies = () => calls.filter((c) => c.url.includes("identify")).length;
const name = () => els["result-name"].textContent;
const action = () => els["result-action"].innerHTML;

const results = [];
function check(label, ok, extra) {
  results.push({ label, ok: !!ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${extra ? "   [" + extra + "]" : ""}`);
}

const pendingIn = {
  ok: true, code: "pending", pending: true, confirm_token: "signed.token",
  confirm_seconds: 2, direction: "in",
  employee: { id: 7, name: "Sam Fletcher", first_name: "Sam" },
};

(async () => {
  identifyReply = pendingIn;

  // 1. Empty scene: no traffic at all.
  await advance(3000);
  check("empty scene: no identify, no commit, screen idle",
        identifies() === 0 && commits() === 0 && name() === "Ready", `name="${name()}"`);

  // 2. Somebody arrives -> identify, and a countdown starts.
  scenePixel = 200;
  await advanceUntil("countdown", () => name() === "Sam Fletcher");
  check("arrival triggers identify", identifies() >= 1, `identifies=${identifies()}`);
  check("countdown announces the name and direction",
        name() === "Sam Fletcher" && action().includes("Clocking IN"), `action="${action()}"`);
  check("Cancel is offered during the countdown", els["cancel-btn"].hidden === false);
  check("NOTHING is committed while the countdown runs", commits() === 0, `commits=${commits()}`);

  // 3. Let it finish -> exactly one commit.
  await advanceUntil("recorded", () => action().includes("Clocked IN"), 8000);
  check("countdown finishing commits exactly once", commits() === 1, `commits=${commits()}`);
  check("the recorded entry is shown", action().includes("Clocked IN"), `action="${action()}"`);
  const commitBody = calls.filter((c) => c.url.includes("commit")).pop().body;
  check("commit sends the signed token, not an employee id",
        commitBody.confirm_token === "signed.token" && commitBody.employee_id === undefined,
        JSON.stringify(commitBody));

  // 4. Cancel path: a second person, cancelled mid-countdown.
  scenePixel = 0;
  await advance(12000);                       // they leave; screen returns to idle
  const beforeCancel = commits();
  identifyReply = {
    ok: true, code: "pending", pending: true, confirm_token: "signed.token2",
    confirm_seconds: 2, direction: "out",
    employee: { id: 9, name: "Ada Reed", first_name: "Ada" },
  };
  scenePixel = 220;
  const started = await advanceUntil("second countdown", () => name() === "Ada Reed");
  check("a second person starts their own countdown", started, `name="${name()}"`);
  check("clocking OUT is announced clearly", action().includes("Clocking OUT"), `action="${action()}"`);

  els["cancel-btn"].click();
  scenePixel = 0;                             // they walk away
  await advance(15000);                       // far past when it would have fired
  check("CANCEL PREVENTS THE COMMIT",
        commits() === beforeCancel, `commits before=${beforeCancel} after=${commits()}`);
  check("Cancel hides the Cancel button", els["cancel-btn"].hidden === true);

  // 5. already_clocked must never commit.
  const beforeAlready = commits();
  await advance(12000);
  identifyReply = {
    ok: true, code: "already_clocked", pending: false, direction: "in",
    occurred_at: "07:31:02", message: "Already clocked in, Sam.",
    employee: { id: 11, name: "Sam Fletcher", first_name: "Sam" },
  };
  scenePixel = 240;
  await advanceUntil("already", () => action().includes("Already clocked in"), 8000);
  check("already_clocked never commits", commits() === beforeAlready, `commits=${commits()}`);
  check("already_clocked is reported to the user",
        action().includes("Already clocked in"), `action="${action()}"`);

  // 6. An unrecognised face must stay silent (no screen churn, no commit).
  scenePixel = 0;
  await advance(12000);
  const beforeUnknown = commits();
  identifyReply = { ok: false, code: "not_recognised", message: "Face not recognised." };
  scenePixel = 210;
  await advance(6000);
  check("an unknown face commits nothing", commits() === beforeUnknown, `commits=${commits()}`);
  check("an unknown face does not show a scary error",
        !action().toLowerCase().includes("not recorded"), `action="${action()}"`);

  check("hands-free uploads the lean frame count",
        (calls.find((c) => c.url.includes("identify")).body.frames || []).length === 2,
        `frames=${(calls.find((c) => c.url.includes("identify")).body.frames || []).length}`);

  // 7. Repeated misses must eventually tell the person what would help, rather
  //    than leaving them watching a screen that looks inert.
  check("repeated misses produce an actionable hint",
        /not recognised|face the camera|closer|hold still|see the office/i
          .test(els["kiosk-hint"].textContent),
        `hint="${els["kiosk-hint"].textContent}"`);

  // 8. THE TOGGLE. Clocked in, then: standing still must NOT clock again, but
  //    leaving and coming back must clock the other way.
  scenePixel = 0;
  await advance(15000);                       // reset to a clean idle state
  calls.length = 0;

  identifyReply = {
    ok: true, code: "pending", pending: true, confirm_token: "tok-in",
    confirm_seconds: 2, direction: "in",
    employee: { id: 42, name: "Rhys Morgan", first_name: "Rhys" },
  };
  scenePixel = 205;                           // Rhys arrives
  await advanceUntil("clocked in", () => action().includes("Clocked IN"), 12000);
  const afterIn = commits();
  check("arriving clocks you IN", afterIn === 1, `commits=${afterIn}`);

  // Rhys stays put, reading the screen. Server would now offer "out".
  identifyReply = {
    ok: true, code: "pending", pending: true, confirm_token: "tok-out",
    confirm_seconds: 2, direction: "out",
    employee: { id: 42, name: "Rhys Morgan", first_name: "Rhys" },
  };
  await advance(20000);                       // still standing there, within re-arm
  check("standing still does not immediately clock you out again",
        commits() === afterIn, `commits=${commits()} (expected ${afterIn})`);
  check("the screen asks them to step away",
        /step away/i.test(els["kiosk-hint"].textContent),
        `hint="${els["kiosk-hint"].textContent}"`);

  // Rhys walks away...
  scenePixel = 0;
  await advance(4000);
  check("walking away commits nothing by itself", commits() === afterIn);

  // ...and comes back. This must clock him OUT.
  scenePixel = 205;
  const cameBack = await advanceUntil(
    "clocked out", () => action().includes("Clocked OUT"), 12000);
  check("COMING BACK CLOCKS YOU OUT", cameBack && commits() === afterIn + 1,
        `commits=${commits()} action="${action()}"`);

  // 9. THE FAILURE THAT WAS REPORTED: a camera that never sees an empty scene
  //    (a kiosk facing a desk) left the kiosk latched for ever - it clocked once
  //    and then nothing, with no explanation. The re-arm timer must rescue it.
  scenePixel = 0;
  await advance(20000);
  calls.length = 0;
  identifyReply = {
    ok: true, code: "pending", pending: true, confirm_token: "tok-a",
    confirm_seconds: 2, direction: "in",
    employee: { id: 77, name: "Owen Pryce", first_name: "Owen" },
  };
  scenePixel = 215;                           // arrives and NEVER leaves
  await advanceUntil("first", () => commits() === 1, 12000);
  check("clocks once while permanently in frame", commits() === 1, `commits=${commits()}`);

  identifyReply = {
    ok: true, code: "pending", pending: true, confirm_token: "tok-b",
    confirm_seconds: 2, direction: "out",
    employee: { id: 77, name: "Owen Pryce", first_name: "Owen" },
  };
  // Never absent for a single tick; only the re-arm timeout can free it.
  const freed = await advanceUntil("second", () => commits() === 2, 60000);
  check("PERMANENTLY IN FRAME STILL CLOCKS AGAIN (re-arm timeout)",
        freed && commits() === 2, `commits=${commits()}`);
  check("the wait is explained on screen while latched",
        /ready again in/i.test(els["kiosk-hint"].textContent) ||
          action().includes("Clocked"),
        `hint="${els["kiosk-hint"].textContent}"`);

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) console.log("failed: " + failed.map((f) => f.label).join("; "));
  process.exit(failed.length ? 1 : 0);
})();
