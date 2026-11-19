#!/usr/bin/env node

import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

function usage() {
  console.log(`Usage:
  preview-html-box.mjs --html <file> [options]

Options:
  --html <file>              Required standalone HTML file
  --screenshot <png>         Screenshot output
  --report <json>            JSON report output
  --width <px>               Viewport width, default 817
  --height <px>              Viewport height, default 560
  --chrome <path>            Browser executable (else $HTML_BOX_BROWSER, PATH, playwright cache)
  --init-script <js>         Inject a task-local script before page JavaScript
  --click <selector>         Click selector before assertions
  --expect-active <s=n>      Assert selector count after click; repeatable
  --bound-selector <css>     Major region selector; repeatable
  --text-selector <css>      Text overflow selector; repeatable
  --timeout-ms <ms>          Total preview deadline, default 30000
  --settle-ms <ms>           Wait after load/click, default 80
  --keep-profile             Keep temporary browser profile
  -h, --help                 Show help

Examples:
  preview-html-box.mjs --html box.html --screenshot box.png
  preview-html-box.mjs --html box.html --click '[data-stage="2"]' \\
    --expect-active '.stage.active=2'
`);
}

function requireValue(argv, index, flag) {
  if (index + 1 >= argv.length) throw new Error(`${flag} requires a value`);
  return argv[index + 1];
}

function parseArgs(argv) {
  const args = {
    width: 817,
    height: 560,
    settleMs: 80,
    timeoutMs: 30000,
    expectedActive: [],
    boundSelectors: [],
    textSelectors: [],
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "-h" || arg === "--help") {
      args.help = true;
    } else if (arg === "--html") {
      args.html = requireValue(argv, i, arg);
      i += 1;
    } else if (arg === "--screenshot") {
      args.screenshot = requireValue(argv, i, arg);
      i += 1;
    } else if (arg === "--report") {
      args.report = requireValue(argv, i, arg);
      i += 1;
    } else if (arg === "--width") {
      args.width = Number(requireValue(argv, i, arg));
      i += 1;
    } else if (arg === "--height") {
      args.height = Number(requireValue(argv, i, arg));
      i += 1;
    } else if (arg === "--chrome") {
      args.chrome = requireValue(argv, i, arg);
      i += 1;
    } else if (arg === "--init-script") {
      args.initScript = requireValue(argv, i, arg);
      i += 1;
    } else if (arg === "--click") {
      args.click = requireValue(argv, i, arg);
      i += 1;
    } else if (arg === "--expect-active") {
      const value = requireValue(argv, i, arg);
      const split = value.lastIndexOf("=");
      if (split <= 0) throw new Error(`${arg} must be SELECTOR=COUNT`);
      args.expectedActive.push({
        selector: value.slice(0, split),
        count: Number(value.slice(split + 1)),
      });
      i += 1;
    } else if (arg === "--bound-selector") {
      args.boundSelectors.push(requireValue(argv, i, arg));
      i += 1;
    } else if (arg === "--text-selector") {
      args.textSelectors.push(requireValue(argv, i, arg));
      i += 1;
    } else if (arg === "--timeout-ms") {
      args.timeoutMs = Number(requireValue(argv, i, arg));
      i += 1;
    } else if (arg === "--settle-ms") {
      args.settleMs = Number(requireValue(argv, i, arg));
      i += 1;
    } else if (arg === "--keep-profile") {
      args.keepProfile = true;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (args.help) return args;
  if (!args.html) throw new Error("--html is required");
  for (const [name, value] of [
    ["width", args.width],
    ["height", args.height],
    ["settle-ms", args.settleMs],
    ["timeout-ms", args.timeoutMs],
  ]) {
    if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be positive`);
  }
  for (const expectation of args.expectedActive) {
    if (!Number.isInteger(expectation.count) || expectation.count < 0) {
      throw new Error("--expect-active count must be a non-negative integer");
    }
  }
  return args;
}

export function findBrowser(explicit) {
  const home = os.homedir();
  const candidates = [
    explicit,
    process.env.HTML_BOX_BROWSER,
    process.env.CHROME_BIN,
  ].filter(Boolean);
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    for (const name of ["google-chrome", "chromium", "chromium-browser", "chrome", "headless_shell", "chrome-headless-shell"]) {
      if (dir) candidates.push(path.join(dir, name));
    }
  }
  candidates.push(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  );
  const cache = path.join(home, ".cache", "ms-playwright");
  try {
    const entries = fs.readdirSync(cache).sort().reverse();
    for (const e of entries) {
      for (const sub of ["chrome-linux", "chrome-linux64", "chrome-headless-shell-linux64"]) {
        const base = path.join(cache, e, sub);
        if (e.startsWith("chromium_headless_shell-")) {
          candidates.push(path.join(base, "headless_shell"), path.join(base, "chrome-headless-shell"));
        } else if (e.startsWith("chromium-")) {
          candidates.push(path.join(base, "chrome"));
        }
      }
    }
  } catch { /* no playwright cache */ }
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

async function getFreePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

function bounded(start, label, { deadline = Infinity, timeoutMs = 5000, signal } = {}) {
  const budget = Math.min(timeoutMs, deadline - performance.now());
  return new Promise((resolve, reject) => {
    let cleanup = () => {};
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      cleanup();
      if (error) reject(error);
      else resolve(value);
    };
    const abort = () => finish(signal.reason || new Error(`${label} cancelled`));
    const timer = setTimeout(() => finish(new Error(`${label} deadline exceeded`)), Math.max(0, budget));
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) { abort(); return; }
    if (budget <= 0) { finish(new Error(`${label} deadline exceeded`)); return; }
    try {
      cleanup = start((value) => finish(null, value), (error) => finish(error)) || cleanup;
      if (settled) cleanup();
    } catch (error) { finish(error); }
  });
}

function createLifetime(timeoutMs, processImpl = process) {
  const controller = new AbortController();
  const deadline = performance.now() + timeoutMs;
  const interrupt = () => controller.abort(new Error("preview interrupted"));
  const timer = setTimeout(() => controller.abort(new Error("preview total deadline exceeded")), timeoutMs);
  processImpl.on("SIGINT", interrupt);
  processImpl.on("SIGTERM", interrupt);
  return {
    deadline, signal: controller.signal,
    abort: (error) => controller.abort(error),
    dispose() {
      clearTimeout(timer);
      processImpl.removeListener("SIGINT", interrupt);
      processImpl.removeListener("SIGTERM", interrupt);
    },
  };
}

function delay(ms, limits) {
  return bounded((resolve) => {
    const timer = setTimeout(resolve, ms);
    return () => clearTimeout(timer);
  }, "preview settle", { ...limits, timeoutMs: ms + 1000 });
}

async function waitForJson(url, child, timeoutMs = 10000, limits = {}) {
  const deadline = Math.min(limits.deadline ?? Infinity, performance.now() + timeoutMs);
  let lastError;
  while (performance.now() < deadline) {
    if (limits.signal?.aborted) throw limits.signal.reason;
    if (child.exitCode !== null || child.signalCode != null) {
      throw new Error(`Chrome exited before DevTools was ready: ${child.exitCode}`);
    }
    try {
      return await bounded((resolve, reject) => {
        const controller = new AbortController();
        (limits.fetchImpl || fetch)(url, { signal: controller.signal })
          .then(async (response) => {
            if (!response.ok) throw new Error(`DevTools HTTP ${response.status}`);
            return await response.json();
          }).then(resolve, reject);
        return () => controller.abort();
      }, "DevTools HTTP", { ...limits, deadline, timeoutMs: 1000 });
    } catch (error) {
      lastError = error;
      if (limits.signal?.aborted) throw limits.signal.reason;
    }
    await delay(Math.min(100, Math.max(0, deadline - performance.now())), { ...limits, deadline });
  }
  throw new Error(`Timed out waiting for DevTools: ${lastError?.message || url}`);
}

async function connectCdp(webSocketUrl, limits = {}) {
  const socket = new (limits.WebSocketImpl || WebSocket)(webSocketUrl);
  const pending = new Map();
  let nextId = 1;
  let failure;
  const fail = (error) => {
    failure = error;
    for (const request of [...pending.values()]) request.reject(error);
    pending.clear();
  };
  const onClose = () => fail(new Error("CDP socket closed"));
  const onError = () => fail(new Error("CDP socket error"));
  const onAbort = () => { fail(limits.signal.reason); socket.close(); };
  const onMessage = (event) => {
    try {
      const message = JSON.parse(event.data);
      const request = pending.get(message.id);
      if (!request) return;
      if (message.error) request.reject(new Error(JSON.stringify(message.error)));
      else request.resolve(message.result);
    } catch { fail(new Error("CDP invalid message")); }
  };
  socket.addEventListener("message", onMessage);
  socket.addEventListener("close", onClose);
  socket.addEventListener("error", onError);
  try {
    await bounded((resolve, reject) => {
      const closed = () => reject(new Error("CDP socket closed during connect"));
      const errored = () => reject(new Error("CDP socket error during connect"));
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("close", closed, { once: true });
      socket.addEventListener("error", errored, { once: true });
      return () => {
        socket.removeEventListener("open", resolve);
        socket.removeEventListener("close", closed);
        socket.removeEventListener("error", errored);
      };
    }, "CDP connect", limits);
  } catch (error) { socket.close(); throw error; }
  limits.signal?.addEventListener("abort", onAbort, { once: true });

  function send(method, params = {}) {
    if (failure) return Promise.reject(failure);
    return bounded((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      try { socket.send(JSON.stringify({ id, method, params })); }
      catch (error) { reject(error); }
      return () => pending.delete(id);
    }, `CDP ${method}`, limits);
  }
  function close() {
    fail(new Error("CDP connection closed"));
    limits.signal?.removeEventListener("abort", onAbort);
    socket.removeEventListener("message", onMessage);
    socket.removeEventListener("close", onClose);
    socket.removeEventListener("error", onError);
    socket.close();
  }
  return { socket, send, close };
}

async function waitForLoad(socket, send, url, limits = {}) {
  let cancel;
  const loaded = bounded((resolve, reject) => {
    cancel = reject;
    const listener = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.method === "Page.loadEventFired") resolve();
      } catch { reject(new Error("CDP invalid load message")); }
    };
    const closed = () => reject(new Error("CDP socket closed before load"));
    const errored = () => reject(new Error("CDP socket error before load"));
    socket.addEventListener("message", listener);
    socket.addEventListener("close", closed);
    socket.addEventListener("error", errored);
    return () => {
      socket.removeEventListener("message", listener);
      socket.removeEventListener("close", closed);
      socket.removeEventListener("error", errored);
    };
  }, "Page load", { timeoutMs: 10000, ...limits });
  try { await Promise.all([loaded, send("Page.navigate", { url })]); }
  finally { cancel?.(new Error("Page load cancelled")); }
}

function evaluationExpression(args) {
  const defaultBounds = ["#canvas", "main", "header", "section", ".grid", "footer"];
  const defaultText = [
    "h1", "h2", "h3", "p", "li", "td", "th", "button", "label",
    ".card strong", ".card span", ".title", ".detail", ".label",
  ];

  return `(() => {
    const clickSelector = ${JSON.stringify(args.click || "")};
    if (clickSelector) {
      const node = document.querySelector(clickSelector);
      if (!node) throw new Error("click selector not found: " + clickSelector);
      node.click();
    }

    const uniqueExisting = (selectors) => Array.from(new Set(
      selectors.flatMap((selector) =>
        Array.from(document.querySelectorAll(selector)).map((node) => ({ selector, node }))
      ).map((item) => item.node)
    ));

    const boundSelectors = ${JSON.stringify(
      args.boundSelectors.length ? args.boundSelectors : defaultBounds,
    )};
    const textSelectors = ${JSON.stringify(
      args.textSelectors.length ? args.textSelectors : defaultText,
    )};

    const bounds = [];
    for (const selector of boundSelectors) {
      for (const node of document.querySelectorAll(selector)) {
        const rect = node.getBoundingClientRect();
        bounds.push({
          selector,
          left: rect.left,
          top: rect.top,
          right: rect.right,
          bottom: rect.bottom
        });
      }
    }

    const textOverflow = [];
    for (const node of uniqueExisting(textSelectors)) {
      if (node.scrollWidth > node.clientWidth || node.scrollHeight > node.clientHeight) {
        textOverflow.push({
          className: node.className || node.tagName,
          clientWidth: node.clientWidth,
          scrollWidth: node.scrollWidth,
          clientHeight: node.clientHeight,
          scrollHeight: node.scrollHeight,
          text: node.textContent.trim().replace(/\\s+/g, " ").slice(0, 180)
        });
      }
    }

    const expectedCounts = ${JSON.stringify(args.expectedActive)}.map((item) => ({
      selector: item.selector,
      expected: item.count,
      actual: document.querySelectorAll(item.selector).length
    }));

    const longTokens = [];
    for (const node of uniqueExisting(textSelectors)) {
      const style = getComputedStyle(node);
      const wraps = style.overflowWrap === "anywhere" || style.overflowWrap === "break-word" || style.wordBreak === "break-all";
      const own = Array.from(node.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" ");
      for (const token of own.split(/\\s+/)) {
        if (token.length >= 24 && !wraps) {
          longTokens.push({ token: token.slice(0, 60), length: token.length, tag: node.tagName.toLowerCase() });
        }
      }
    }

    return {
      title: document.title,
      viewport: {
        width: document.documentElement.clientWidth,
        height: document.documentElement.clientHeight,
        scrollWidth: document.documentElement.scrollWidth,
        scrollHeight: document.documentElement.scrollHeight
      },
      bounds,
      textOverflow,
      longTokens,
      expectedCounts,
      bodyTextLength: document.body.innerText.trim().length
    };
  })()`;
}

// Isolate even regular-file IO: filesystem stalls must not block the deadline timer.
const fileIoProgram = `
import fs from "node:fs";
const [operation, file] = process.argv.slice(1);
let fd;
try {
  const writing = operation === "write";
  const chunks = [];
  if (writing) for await (const chunk of process.stdin) chunks.push(chunk);
  fd = fs.openSync(file, fs.constants.O_NONBLOCK | fs.constants.O_NOCTTY |
    (writing ? fs.constants.O_WRONLY | fs.constants.O_CREAT : fs.constants.O_RDONLY), 0o666);
  if (!fs.fstatSync(fd).isFile()) throw new Error("IO target must be a regular file");
  if (writing) {
    fs.ftruncateSync(fd, 0);
    fs.writeFileSync(fd, Buffer.concat(chunks));
  } else process.stdout.write(fs.readFileSync(fd));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally { if (fd !== undefined) fs.closeSync(fd); }
`;

async function fileIo(operation, file, data, limits) {
  return await bounded((resolve, reject) => {
    const child = spawn(process.execPath, ["--input-type=module", "-e", fileIoProgram, operation, file],
      { stdio: ["pipe", "pipe", "pipe"] });
    const chunks = [];
    let stderr = "";
    child.stdout.on("data", (chunk) => chunks.push(chunk));
    child.stderr.on("data", (chunk) => { stderr = (stderr + chunk).slice(-4096); });
    child.once("error", reject);
    child.stdin.on("error", reject);
    child.once("close", (code) => {
      if (code === 0) resolve(Buffer.concat(chunks));
      else reject(new Error(`file IO: ${stderr.trim() || "owned IO process failed"}`));
    });
    child.stdin.end(data);
    return () => {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
      child.stdin.destroy();
      child.stdout.destroy();
      child.stderr.destroy();
    };
  }, `file IO ${operation}`, { ...limits, timeoutMs: Infinity });
}

async function main() {
  const startedAt = performance.now();
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    usage();
    return;
  }

  const lifetime = createLifetime(Math.max(0, startedAt + args.timeoutMs - performance.now()));
  let child, profile, cdp;
  try {
    const htmlPath = path.resolve(args.html);
    const html = (await fileIo("read", htmlPath, undefined, lifetime)).toString("utf8");
    if (!/<html[\s>]/i.test(html)) throw new Error("input does not contain an html element");
    const initScriptPath = args.initScript ? path.resolve(args.initScript) : null;
    const initScript = initScriptPath
      ? (await fileIo("read", initScriptPath, undefined, lifetime)).toString("utf8")
      : null;

    const chrome = findBrowser(args.chrome);
    if (!chrome) throw new Error("no Chromium found; pass --chrome or set HTML_BOX_BROWSER");
    const port = await bounded((resolve, reject) => {
      getFreePort().then(resolve, reject);
    }, "DevTools port", lifetime);
    profile = fs.mkdtempSync(path.join(os.tmpdir(), "html-box-"));
    child = spawn(
      chrome,
      [
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        `--remote-debugging-port=${port}`,
        `--user-data-dir=${profile}`,
        "about:blank",
      ],
      { stdio: ["ignore", "ignore", "pipe"] },
    );

    child.once("error", (error) => lifetime.abort(error));
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk.toString()).slice(-65536);
    });
    const targets = await waitForJson(`http://127.0.0.1:${port}/json`, child, 10000, lifetime);
    const target = targets.find((item) => item.type === "page");
    assert.ok(target, "Chrome did not expose a page target");
    cdp = await connectCdp(target.webSocketDebuggerUrl, lifetime);
    const { socket, send } = cdp;

    await send("Page.enable");
    await send("Runtime.enable");
    if (initScript) {
      await send("Page.addScriptToEvaluateOnNewDocument", {
        source: initScript,
      });
    }
    await send("Emulation.setDeviceMetricsOverride", {
      width: args.width,
      height: args.height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await waitForLoad(socket, send, pathToFileURL(htmlPath).href, lifetime);
    await delay(args.settleMs, lifetime);

    const evaluated = await send("Runtime.evaluate", {
      returnByValue: true,
      expression: evaluationExpression(args),
    });
    if (evaluated.exceptionDetails) {
      throw new Error(evaluated.exceptionDetails.text || "Runtime.evaluate failed");
    }
    const report = evaluated.result.value;

    assert.deepEqual(report.viewport, {
      width: args.width,
      height: args.height,
      scrollWidth: args.width,
      scrollHeight: args.height,
    });
    for (const rect of report.bounds) {
      assert.ok(rect.left >= 0, `${rect.selector} extends left of viewport`);
      assert.ok(rect.right <= args.width, `${rect.selector} extends right of viewport`);
      assert.ok(rect.top >= 0, `${rect.selector} extends above viewport`);
      assert.ok(rect.bottom <= args.height, `${rect.selector} extends below viewport`);
    }
    assert.deepEqual(report.textOverflow, []);
    for (const count of report.expectedCounts) {
      assert.equal(
        count.actual,
        count.expected,
        `${count.selector} expected ${count.expected}, got ${count.actual}`,
      );
    }
    assert.ok(report.bodyTextLength > 0, "page body is blank");

    if (args.screenshot) {
      const screenshot = await send("Page.captureScreenshot", {
        format: "png",
        fromSurface: true,
      });
      await fileIo("write", path.resolve(args.screenshot),
        Buffer.from(screenshot.data, "base64"), lifetime);
    }

    const finalReport = {
      ...report,
      html: htmlPath,
      htmlSha256: crypto.createHash("sha256").update(html).digest("hex"),
      initScript: initScriptPath,
      screenshot: args.screenshot ? path.resolve(args.screenshot) : null,
    };
    if (args.report) {
      await fileIo("write", path.resolve(args.report),
        `${JSON.stringify(finalReport, null, 2)}\n`, lifetime);
    }
    process.stdout.write(`${JSON.stringify(finalReport, null, 2)}\n`);
  } finally {
    try {
      try { cdp?.close(); }
      finally {
        try { if (child) await stopChrome(child); }
        finally { if (profile && !args.keepProfile) fs.rmSync(profile, { recursive: true, force: true }); }
      }
    } finally { lifetime.dispose(); }
  }
}

async function stopChrome(child, graceMs = 1500) {
  await new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) {
      resolve();
      return;
    }
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener("exit", finish);
      resolve();
    };
    const timer = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill("SIGKILL");
      }
      finish();
    }, graceMs);
    child.once("exit", finish);
    child.kill("SIGTERM");
  });
  child.stderr?.destroy();
}

export { bounded, connectCdp, createLifetime, delay, stopChrome, waitForJson, waitForLoad };

if (process.argv[1] && fs.existsSync(process.argv[1]) &&
    fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))) {
  try {
    await main();
  } catch (error) {
    console.error(`preview-html-box: ${error.message}`);
    process.exitCode = 1;
  }
}
