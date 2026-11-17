#!/usr/bin/env node
// Static source checks for a standalone HTML box. Node >= 18, no dependencies.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

function usage() {
  console.log(`Usage:
  validate-html-box-source.mjs --html <file> [options]

Options:
  --html <file>      Required standalone HTML file
  --width <px>       Expected fixed width, default 817
  --height <px>      Expected fixed height, default 560
  --bridge           Require a guarded optional host bridge (window.htmlBoxHost)
  --allow-network    Permit external resources and network APIs
  --allow-storage    Permit localStorage/sessionStorage
  -h, --help         Show help
`);
}

export function parseArgs(argv) {
  const args = { width: 817, height: 560 };
  const need = (i, flag) => {
    if (i + 1 >= argv.length) throw new Error(`${flag} requires a value`);
    return argv[i + 1];
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "-h" || arg === "--help") args.help = true;
    else if (arg === "--bridge") args.bridge = true;
    else if (arg === "--allow-network") args.allowNetwork = true;
    else if (arg === "--allow-storage") args.allowStorage = true;
    else if (arg === "--html") args.html = need(i++, arg);
    else if (arg === "--width") args.width = Number(need(i++, arg));
    else if (arg === "--height") args.height = Number(need(i++, arg));
    else throw new Error(`Unknown argument: ${arg}`);
  }
  for (const k of ["width", "height"]) {
    if (!Number.isInteger(args[k]) || args[k] <= 0) throw new Error(`--${k} must be a positive integer`);
  }
  return args;
}

const matches = (value, pattern) => Array.from(value.matchAll(pattern), (m) => m[0]);

export function validateHtml(html, opts = {}) {
  const width = opts.width ?? 817;
  const height = opts.height ?? 560;
  const errors = [];
  const warnings = [];

  if (!/<!doctype\s+html/i.test(html)) errors.push("missing <!doctype html>");
  if (!/<html[\s>]/i.test(html)) errors.push("missing html element");
  if (!/<title>\s*[^<\s][^<]*<\/title>/i.test(html)) errors.push("missing non-empty title");
  if (!/<meta\s+name=["']viewport["']/i.test(html)) errors.push("missing viewport meta");

  const dim = (prop, n) =>
    new RegExp(`(?:html|body)[^{}]*\\{[^}]*\\b${prop}\\s*:\\s*${n}px`, "is").test(html);
  if (!dim("width", width)) errors.push(`html/body must set an explicit width of ${width}px`);
  if (!dim("height", height)) errors.push(`html/body must set an explicit height of ${height}px`);

  if (!opts.allowStorage && /\b(?:localStorage|sessionStorage)\b/.test(html)) {
    errors.push("localStorage/sessionStorage can throw in sandboxed frames; keep state in memory or use a host capability");
  }

  const external = matches(
    html,
    /<(?:script|link|img|source|video|audio|iframe)\b[^>]*(?:src|href)=["'](?:https?:)?\/\/[^"']+["'][^>]*>|url\(\s*["']?(?:https?:)?\/\/[^)"']+["']?\s*\)|@import\s+["']?https?:/gi,
  );
  if (external.length && !opts.allowNetwork) {
    errors.push(`external network resources are not allowed: ${external.length}`);
  }
  const netApis = matches(html, /\b(?:fetch\s*\(|XMLHttpRequest|new\s+WebSocket|new\s+EventSource|sendBeacon\s*\()/g);
  if (netApis.length && !opts.allowNetwork) {
    errors.push(`network APIs are not allowed without --allow-network: ${Array.from(new Set(netApis)).join(", ")}`);
  }
  if (netApis.length && !/(?:addEventListener\s*\(\s*["'](?:click|submit|keydown)|\bonclick\s*=)/i.test(html)) {
    errors.push("network calls require an explicit user gesture handler");
  }

  const secretPatterns = [
    /\b(?:app[_-]?secret|api[_-]?key|client[_-]?secret)\s*[:=]\s*["'][^"']+["']/i,
    /\b(?:access|refresh|bearer|auth)[_-]?token\s*[:=]\s*["'][^"']+["']/i,
    /\bAuthorization["']?\s*[:=]\s*["']Bearer\s+[^"']+["']/i,
    /\bsk-[A-Za-z0-9]{20,}/,
  ];
  if (secretPatterns.some((p) => p.test(html))) errors.push("possible hard-coded credential or token");

  const usesBridge = /\bwindow\.htmlBoxHost\b/.test(html);
  if (opts.bridge && !usesBridge) errors.push("--bridge requires at least one window.htmlBoxHost use");
  if (usesBridge) {
    const guard =
      /if\s*\([^)]*(?:!window\.htmlBoxHost|!host\b|typeof\s+window\.htmlBoxHost|typeof\s+host)/s.test(html) ||
      /window\.htmlBoxHost\s*(?:\?\.|\|\||\?\?|&&)/.test(html);
    if (!guard) errors.push("window.htmlBoxHost usage must have an unavailable-host guard");
    if (!/(?:catch\s*\(|\.catch\s*\()/s.test(html)) errors.push("host bridge usage must expose an error path");
  }

  if (!/overflow\s*:\s*hidden/i.test(html)) {
    warnings.push("no overflow:hidden rule found; browser verification is required");
  }
  if (!/overflow-wrap\s*:\s*(?:anywhere|break-word)|word-break\s*:\s*break-all/i.test(html)) {
    warnings.push("no overflow-wrap rule found; long tokens may overflow");
  }

  return {
    valid: errors.length === 0,
    bridge: usesBridge,
    external_resources: external.length,
    errors,
    warnings,
  };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) return usage();
  if (!args.html) throw new Error("--html is required");
  const htmlPath = path.resolve(args.html);
  const result = { html: htmlPath, ...validateHtml(fs.readFileSync(htmlPath, "utf8"), args) };
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (!result.valid) process.exitCode = 1;
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  try {
    main();
  } catch (error) {
    console.error(`validate-html-box-source: ${error.message}`);
    process.exitCode = 1;
  }
}
