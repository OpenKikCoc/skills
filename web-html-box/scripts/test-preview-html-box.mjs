#!/usr/bin/env node

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { connectCdp, createLifetime, stopChrome, waitForJson, waitForLoad } from "./preview-html-box.mjs";

class FakeSocket extends EventTarget {
  constructor() {
    super();
    this.closed = false;
    queueMicrotask(() => this.dispatchEvent(new Event("open")));
  }
  send(value) { this.lastRequest = JSON.parse(value); }
  close() { this.closed = true; this.dispatchEvent(new Event("close")); }
  message(value) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) })); }
}

const limits = { timeoutMs: 30, WebSocketImpl: FakeSocket };
async function guarded(promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("TEST WATCHDOG: operation hung")), 400);
    })]);
  } finally { clearTimeout(timer); }
}

// Unit transports are in-memory fakes; entrypoint tests start only owned fake processes.
test("connect deadline closes a socket that never opens", async () => {
  let socket;
  class NeverOpen extends FakeSocket {
    constructor() {
      super(); socket = this;
    }
    dispatchEvent(event) { return event.type === "open" ? true : super.dispatchEvent(event); }
  }
  await assert.rejects(guarded(connectCdp("fake", { ...limits, WebSocketImpl: NeverOpen })), /connect deadline/);
  assert.equal(socket.closed, true);
});

test("every CDP request times out when no response arrives", async () => {
  const cdp = await connectCdp("fake", limits);
  try {
    await assert.rejects(guarded(cdp.send("Runtime.evaluate")), /CDP Runtime.evaluate deadline/);
    const next = cdp.send("Page.enable");
    cdp.socket.message({ id: cdp.socket.lastRequest.id, result: { enabled: true } });
    assert.deepEqual(await next, { enabled: true });
  } finally { cdp.close(); }
});

for (const event of ["close", "error"]) {
  test(`disconnect ${event} rejects all pending and future requests`, async () => {
    const cdp = await connectCdp("fake", { ...limits, timeoutMs: 200 });
    const first = cdp.send("Page.enable");
    const second = cdp.send("Runtime.enable");
    cdp.socket.dispatchEvent(new Event(event));
    await assert.rejects(guarded(first), /socket/);
    await assert.rejects(guarded(second), /socket/);
    await assert.rejects(cdp.send("Page.navigate"), /socket/);
    cdp.close();
  });
}

test("load wait has a deadline even after navigate succeeds", async () => {
  const socket = new FakeSocket();
  await assert.rejects(guarded(waitForLoad(socket, async () => ({}), "fake", limits)), /Page load deadline/);
});

test("load disconnect fails immediately and successful load remains supported", async () => {
  const socket = new FakeSocket();
  await assert.rejects(waitForLoad(socket, async () => { socket.close(); }, "fake", limits), /closed before load/);
  const loaded = new FakeSocket();
  await waitForLoad(loaded, async () => {
    loaded.message({ method: "Page.loadEventFired" });
  }, "fake", limits);
});

test("HTTP headers and body are bounded and receive AbortSignal", async () => {
  for (const stage of ["headers", "body"]) {
    let signal;
    const fetchImpl = async (_url, options) => {
      signal = options.signal;
      if (stage === "headers") return await new Promise(() => {});
      return { ok: true, json: () => new Promise(() => {}) };
    };
    await assert.rejects(guarded(waitForJson("fake", { exitCode: null }, 30, { fetchImpl })), /deadline|Timed out/);
    assert.ok(signal instanceof AbortSignal);
    assert.equal(signal.aborted, true);
  }
});

test("HTTP success preserves target JSON", async () => {
  const value = [{ type: "page" }];
  assert.deepEqual(await waitForJson("fake", { exitCode: null }, 30, {
    fetchImpl: async () => ({ ok: true, json: async () => value }),
  }), value);
});

for (const trigger of ["deadline", "SIGINT", "SIGTERM"]) {
  test(`${trigger} cancels pending work and reaches TERM then KILL cleanup`, async () => {
    const processImpl = new EventEmitter();
    const lifetime = createLifetime(trigger === "deadline" ? 30 : 1000, processImpl);
    const child = new EventEmitter();
    child.exitCode = child.signalCode = null;
    const kills = [];
    child.kill = (signal) => { kills.push(signal); };
    let destroyed = false;
    child.stderr = { destroy() { destroyed = true; } };
    let cdp;
    try {
      cdp = await connectCdp("fake", { ...lifetime, WebSocketImpl: FakeSocket });
      const waiting = cdp.send("Runtime.evaluate");
      if (trigger !== "deadline") processImpl.emit(trigger);
      // The total-budget RPC timer and lifetime abort may expire in either order.
      await assert.rejects(guarded(waiting), /interrupted|deadline/);
    } finally {
      cdp?.close();
      try { await stopChrome(child, 10); }
      finally { lifetime.dispose(); }
    }
    assert.deepEqual(kills, ["SIGTERM", "SIGKILL"]);
    assert.equal(destroyed, true);
    assert.equal(processImpl.listenerCount("SIGINT"), 0);
    assert.equal(processImpl.listenerCount("SIGTERM"), 0);
  });
}

test("cleanup does not kill a child that already exited", async () => {
  const child = new EventEmitter();
  child.exitCode = 0; child.signalCode = null;
  child.kill = () => assert.fail("must not signal an exited child");
  await stopChrome(child, 10);
});

const previewPath = fileURLToPath(new URL("./preview-html-box.mjs", import.meta.url));
function entryFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "preview-entry-test-"));
  const html = path.join(root, "input.html");
  const chrome = path.join(root, "fake-chrome.mjs");
  const preload = path.join(root, "preload.mjs");
  fs.writeFileSync(html, "<html><body>fake</body></html>");
  fs.writeFileSync(chrome, `#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
const root = process.env.PREVIEW_TEST_ROOT;
process.on("SIGTERM", () => {
  fs.writeFileSync(path.join(root, "term"), "ready");
  if (process.env.PREVIEW_TEST_MODE !== "hang") process.exit(0);
});
fs.writeFileSync(path.join(root, "pid"), String(process.pid));
setInterval(() => {}, 20);
`, { mode: 0o700 });
  fs.writeFileSync(preload, `import net from "node:net";
import fs from "node:fs";
import path from "node:path";
import { EventEmitter } from "node:events";
const stalled = process.env.PREVIEW_TEST_IO_STALL;
if (stalled) {
  const method = stalled === "read" ? "readFileSync" : "writeFileSync";
  const original = fs[method];
  const writeMarker = fs.writeFileSync;
  fs[method] = (...args) => {
    const root = process.env.PREVIEW_TEST_ROOT;
    const target = path.join(root, stalled === "read" ? "input.html" : "report.json");
    if (args[0] === target || process.argv[1] === stalled) {
      writeMarker(path.join(root, "io-pid"), String(process.pid));
      while (true) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1000);
    }
    return original(...args);
  };
}
net.createServer = () => {
  const server = new EventEmitter();
  server.listen = (_port, _host, ready) => queueMicrotask(ready);
  server.address = () => ({ port: 12345 });
  server.close = (done) => queueMicrotask(done);
  return server;
};
globalThis.fetch = async (_url, { signal }) => {
  if (process.env.PREVIEW_TEST_MODE === "hang") {
    return await new Promise((_, reject) => signal.addEventListener("abort",
      () => reject(new Error("fake abort")), { once: true }));
  }
  return { ok: true, json: async () => [{ type: "page", webSocketDebuggerUrl: "fake" }] };
};
globalThis.WebSocket = class extends EventTarget {
  constructor() { super(); setImmediate(() => this.dispatchEvent(new Event("open"))); }
  send(value) {
    const { id, method } = JSON.parse(value);
    let result = {};
    if (method === "Runtime.evaluate") result = { result: { value: {
      viewport: { width: 817, height: 560, scrollWidth: 817, scrollHeight: 560 },
      bounds: [], textOverflow: [], expectedCounts: [], bodyTextLength: 4,
    } } };
    if (method === "Page.captureScreenshot") result = { data: Buffer.from("fake png").toString("base64") };
    setImmediate(() => {
      this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ id, result }) }));
      if (method === "Page.navigate") this.dispatchEvent(new MessageEvent("message", {
        data: JSON.stringify({ method: "Page.loadEventFired" }),
      }));
    });
  }
  close() { this.dispatchEvent(new Event("close")); }
};
`);
  return { root, html, chrome, preload };
}

function childRunning(pid) {
  try {
    process.kill(pid, 0);
    if (process.platform === "linux") {
      return fs.readFileSync(`/proc/${pid}/stat`, "utf8").split(" ")[2] !== "Z";
    }
    return true;
  } catch (error) {
    if (error.code === "ESRCH" || error.code === "ENOENT") return false;
    throw error;
  }
}

async function runEntry(fixture, args, { entry = previewPath, mode = "success", signal, ioStall } = {}) {
  const child = spawn(process.execPath, ["--import", fixture.preload, entry, ...args], {
    env: { ...process.env, PREVIEW_TEST_ROOT: fixture.root, PREVIEW_TEST_MODE: mode,
      ...(ioStall ? { PREVIEW_TEST_IO_STALL: ioStall, NODE_OPTIONS: `--import=${fixture.preload}` } : {}) },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "", stderr = "", watchdog = false;
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const timer = setTimeout(() => { watchdog = true; child.kill("SIGKILL"); }, 5000);
  const marker = signal && setInterval(() => {
    if (fs.existsSync(path.join(fixture.root, "term"))) {
      clearInterval(marker);
      child.kill(signal);
      child.kill(signal);
    }
  }, 5);
  try {
    const result = await new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code, exitSignal) => resolve({ code, exitSignal }));
    });
    const pidFile = path.join(fixture.root, "pid");
    const pid = fs.existsSync(pidFile) ? Number(fs.readFileSync(pidFile, "utf8")) : null;
    const childLive = pid !== null && childRunning(pid);
    if (childLive) process.kill(pid, "SIGKILL");
    return { ...result, stdout, stderr, watchdog, childLive };
  } finally {
    clearTimeout(timer);
    clearInterval(marker);
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  }
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  test(`entrypoint ${signal} during cleanup grace still reaps owned Chrome`, async () => {
    const fixture = entryFixture();
    try {
      const result = await runEntry(fixture, ["--html", fixture.html, "--chrome", fixture.chrome,
        "--timeout-ms", "600"], { mode: "hang", signal });
      assert.equal(result.watchdog, false);
      assert.equal(result.code, 1, JSON.stringify(result));
      assert.equal(result.exitSignal, null);
      assert.equal(result.childLive, false);
      assert.ok(fs.existsSync(path.join(fixture.root, "term")));
    } finally { fs.rmSync(fixture.root, { recursive: true, force: true }); }
  });
}

for (const flag of ["--html", "--init-script", "--report", "--screenshot"]) {
  test(`entrypoint rejects ${flag} FIFO without blocking`, async () => {
    const fixture = entryFixture();
    try {
      const fifo = path.join(fixture.root, "file.fifo");
      assert.equal(spawnSync("mkfifo", [fifo]).status, 0);
      const args = ["--html", fixture.html, "--chrome", fixture.chrome, "--settle-ms", "1",
        "--timeout-ms", "1000", flag, fifo];
      const result = await runEntry(fixture, args);
      assert.equal(result.watchdog, false, "FIFO blocked the real entrypoint");
      assert.equal(result.code, 1);
      assert.match(result.stderr, /regular file|ENXIO/);
      assert.equal(result.childLive, false);
      assert.ok(fs.statSync(fifo).isFIFO());
    } finally { fs.rmSync(fixture.root, { recursive: true, force: true }); }
  });
}

for (const operation of ["read", "write"]) {
  test(`entrypoint bounds stalled regular-file ${operation} and kills its owned IO process`, async () => {
    const fixture = entryFixture();
    try {
      const result = await runEntry(fixture, ["--html", fixture.html, "--chrome", fixture.chrome,
        "--settle-ms", "1", "--timeout-ms", "700", "--report", path.join(fixture.root, "report.json")],
      { ioStall: operation });
      assert.equal(result.watchdog, false);
      assert.equal(result.code, 1);
      assert.match(result.stderr, /deadline/);
      assert.equal(result.childLive, false);
      const ioPid = Number(fs.readFileSync(path.join(fixture.root, "io-pid"), "utf8"));
      assert.equal(childRunning(ioPid), false);
    } finally { fs.rmSync(fixture.root, { recursive: true, force: true }); }
  });
}

for (const flag of ["--html", "--report"]) {
  test(`entrypoint rejects ${flag} device`, async () => {
    const fixture = entryFixture();
    try {
      const result = await runEntry(fixture, ["--html", fixture.html, "--chrome", fixture.chrome,
        "--settle-ms", "1", "--timeout-ms", "1000", flag, "/dev/null"]);
      assert.equal(result.watchdog, false);
      assert.equal(result.code, 1);
      assert.match(result.stderr, /regular file/);
      assert.equal(result.childLive, false);
    } finally { fs.rmSync(fixture.root, { recursive: true, force: true }); }
  });
}

for (const kind of ["direct", "file symlink", "directory symlink"]) {
  for (const action of ["help", "invalid input", "successful fake preview"]) {
  test(`${kind} entrypoint runs ${action}`, async () => {
    const fixture = entryFixture();
    try {
      let entry = previewPath;
      if (kind === "file symlink") {
        entry = path.join(fixture.root, "preview.mjs");
        fs.symlinkSync(previewPath, entry);
      } else if (kind === "directory symlink") {
        const directory = path.join(fixture.root, "scripts");
        fs.symlinkSync(path.dirname(previewPath), directory, "dir");
        entry = path.join(directory, path.basename(previewPath));
      }
      if (action === "help") {
        const help = await runEntry(fixture, ["--help"], { entry });
        assert.equal(help.code, 0);
        assert.match(help.stdout, /Usage:/);
        return;
      }
      if (action === "invalid input") {
        const invalid = await runEntry(fixture, ["--html", path.join(fixture.root, "missing.html")], { entry });
        assert.equal(invalid.code, 1);
        assert.match(invalid.stderr, /ENOENT/);
        return;
      }
      const report = path.join(fixture.root, "report.json");
      const screenshot = path.join(fixture.root, "screenshot.png");
      const success = await runEntry(fixture, ["--html", fixture.html, "--chrome", fixture.chrome,
        "--settle-ms", "1", "--timeout-ms", "3000", "--report", report, "--screenshot", screenshot], { entry });
      assert.equal(success.code, 0, success.stderr);
      assert.equal(success.watchdog, false);
      assert.equal(success.childLive, false);
      assert.equal(JSON.parse(success.stdout).bodyTextLength, 4);
      assert.equal(JSON.parse(fs.readFileSync(report)).bodyTextLength, 4);
      assert.equal(fs.readFileSync(screenshot, "utf8"), "fake png");
    } finally { fs.rmSync(fixture.root, { recursive: true, force: true }); }
  });
  }
}
