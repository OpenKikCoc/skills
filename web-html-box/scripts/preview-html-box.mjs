#!/usr/bin/env node

import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";

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

async function waitForJson(url, child, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Chrome exited before DevTools was ready: ${child.exitCode}`);
    }
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for DevTools: ${lastError?.message || url}`);
}

async function connectCdp(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  const pending = new Map();
  let nextId = 1;

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result);
  });

  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });

  function send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }

  return { socket, send };
}

async function waitForLoad(socket, send, url) {
  const loaded = new Promise((resolve) => {
    const listener = (event) => {
      const message = JSON.parse(event.data);
      if (message.method !== "Page.loadEventFired") return;
      socket.removeEventListener("message", listener);
      resolve();
    };
    socket.addEventListener("message", listener);
  });
  await send("Page.navigate", { url });
  await loaded;
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

    const expectedCounts = ${JSON.stringify(args.expectedActive)}.map((item) => ({
      selector: item.selector,
      expected: item.count,
      actual: document.querySelectorAll(item.selector).length
    }));

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

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    usage();
    return;
  }

  const htmlPath = path.resolve(args.html);
  const html = fs.readFileSync(htmlPath, "utf8");
  if (!/<html[\s>]/i.test(html)) throw new Error("input does not contain an html element");
  const initScriptPath = args.initScript ? path.resolve(args.initScript) : null;
  const initScript = initScriptPath
    ? fs.readFileSync(initScriptPath, "utf8")
    : null;

  const chrome = findBrowser(args.chrome);
  if (!chrome) throw new Error("no Chromium found; pass --chrome or set HTML_BOX_BROWSER");
  const port = await getFreePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "html-box-"));
  const child = spawn(
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

  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  try {
    const targets = await waitForJson(`http://127.0.0.1:${port}/json`, child);
    const target = targets.find((item) => item.type === "page");
    assert.ok(target, "Chrome did not expose a page target");
    const { socket, send } = await connectCdp(target.webSocketDebuggerUrl);

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
    await waitForLoad(socket, send, pathToFileURL(htmlPath).href);
    await new Promise((resolve) => setTimeout(resolve, args.settleMs));

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
      fs.writeFileSync(
        path.resolve(args.screenshot),
        Buffer.from(screenshot.data, "base64"),
      );
    }

    const finalReport = {
      ...report,
      html: htmlPath,
      htmlSha256: crypto.createHash("sha256").update(html).digest("hex"),
      initScript: initScriptPath,
      screenshot: args.screenshot ? path.resolve(args.screenshot) : null,
    };
    if (args.report) {
      fs.writeFileSync(
        path.resolve(args.report),
        `${JSON.stringify(finalReport, null, 2)}\n`,
      );
    }
    process.stdout.write(`${JSON.stringify(finalReport, null, 2)}\n`);
    socket.close();
  } finally {
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
        resolve();
      };
      const timer = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) {
          child.kill("SIGKILL");
        }
        finish();
      }, 1500);
      child.once("exit", finish);
      child.kill("SIGTERM");
    });
    if (!args.keepProfile) fs.rmSync(profile, { recursive: true, force: true });
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  try {
    await main();
  } catch (error) {
    console.error(`preview-html-box: ${error.message}`);
    process.exitCode = 1;
  }
}
