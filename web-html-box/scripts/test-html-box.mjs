#!/usr/bin/env node
// Run: node scripts/test-html-box.mjs   (node >= 18, no dependencies)
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { validateHtml } from "./validate-html-box-source.mjs";
import {
  sha256, htmlFromRecord, writeArchive, verifyReadback, titleFromHtml,
} from "./archive-html-box.mjs";
import { findBrowser } from "./preview-html-box.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const assets = path.join(here, "..", "assets");
const plain = path.join(assets, "html-box-817x560.html");
const hosted = path.join(assets, "html-box-host-817x560.html");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "html-box-test-"));
test.after(() => fs.rmSync(tmp, { recursive: true, force: true }));

const good = (extra = "", body = "<p>hi</p>") => `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=817"><title>T</title>
<style>html,body{width:817px;height:560px;overflow:hidden;overflow-wrap:anywhere}</style></head>
<body>${body}${extra}</body></html>`;

test("validator accepts both shipped templates", () => {
  for (const f of [plain, hosted]) {
    const r = validateHtml(fs.readFileSync(f, "utf8"));
    assert.deepEqual(r.errors, [], f);
  }
  assert.equal(validateHtml(fs.readFileSync(hosted, "utf8"), { bridge: true }).valid, true);
});

test("validator fails on structural problems", () => {
  assert.ok(validateHtml("<html><body>x</body></html>").errors.length >= 3);
  assert.ok(validateHtml(good().replace("height:560px", "")).errors.some((e) => /height/.test(e)));
  assert.ok(validateHtml(good().replace(/<title>T<\/title>/, "<title></title>")).errors.some((e) => /title/.test(e)));
});

test("validator honours a custom viewport", () => {
  const html = good().replace("817px", "640px").replace("560px", "400px");
  assert.equal(validateHtml(html, { width: 640, height: 400 }).valid, true);
  assert.equal(validateHtml(html).valid, false);
});

test("validator rejects storage, network, and credentials", () => {
  assert.ok(validateHtml(good("<script>localStorage.x=1</script>")).errors.length);
  assert.equal(validateHtml(good("<script>localStorage.x=1</script>"), { allowStorage: true }).valid, true);
  assert.ok(validateHtml(good('<img src="https://example.com/a.png">')).errors.length);
  assert.equal(validateHtml(good('<img src="https://example.com/a.png">'), { allowNetwork: true }).valid, true);
  assert.ok(validateHtml(good("<script>fetch('/x')</script>")).errors.length);
  assert.ok(validateHtml(good('<script>const api_key = "abc123";</script>')).errors.some((e) => /credential/.test(e)));
});

test("validator requires a guard and error path for the host bridge", () => {
  const unguarded = good("<script>window.htmlBoxHost.getContext()</script>");
  assert.ok(validateHtml(unguarded).errors.some((e) => /guard/.test(e)));
  const guarded = good(`<script>async function f(){ try { if (!window.htmlBoxHost) return; await window.htmlBoxHost.getContext(); } catch (e) {} }</script>`);
  assert.equal(validateHtml(guarded, { bridge: true }).valid, true);
  assert.ok(validateHtml(good(), { bridge: true }).errors.some((e) => /--bridge/.test(e)));
});

test("archive writes a manifest whose hash matches the bytes", () => {
  const html = fs.readFileSync(plain, "utf8");
  const dir = path.join(tmp, "a1");
  const m = writeArchive(dir, html, { targetKind: "file", targetId: "x" });
  assert.equal(m.html_sha256, sha256(html));
  assert.equal(m.title, "HTML Box Title");
  assert.equal(m.html_bytes, Buffer.byteLength(html));
  assert.equal(sha256(fs.readFileSync(path.join(dir, "index.html"))), m.html_sha256);
});

test("verify passes on identical read-back and fails on any difference", () => {
  const html = good();
  const dir = path.join(tmp, "a2");
  writeArchive(dir, html);
  const same = path.join(tmp, "same.html");
  fs.writeFileSync(same, html);
  assert.equal(verifyReadback(dir, same).ok, true);
  const nl = path.join(tmp, "nl.html");
  fs.writeFileSync(nl, `${html}\n`);
  assert.equal(verifyReadback(dir, nl).ok, false);
  const other = path.join(tmp, "other.html");
  fs.writeFileSync(other, html.replace("hi", "ho"));
  assert.equal(verifyReadback(dir, other).ok, false);
});

test("verify detects a tampered archive copy", () => {
  const html = good();
  const dir = path.join(tmp, "a3");
  writeArchive(dir, html);
  fs.appendFileSync(path.join(dir, "index.html"), " ");
  const rb = path.join(tmp, "rb3.html");
  fs.writeFileSync(rb, html);
  assert.equal(verifyReadback(dir, rb).ok, false);
});

test("extract handles direct, JSON-string, and object records", () => {
  const html = good();
  const record = { html, extra: 1 };
  assert.equal(htmlFromRecord({ data: { html } }, "data.html").html, html);
  assert.equal(htmlFromRecord({ r: JSON.stringify(record) }, "r").html, html);
  assert.deepEqual(htmlFromRecord({ r: record }, "r").record, record);
  assert.throws(() => htmlFromRecord({ r: "nope" }, "r"), /standalone HTML/);
  assert.throws(() => htmlFromRecord({}, "missing.path"), /not found/);
  assert.equal(titleFromHtml(html), "T");
});

test("archive CLI round trip", () => {
  const script = path.join(here, "archive-html-box.mjs");
  const out = path.join(tmp, "cli");
  let r = spawnSync("node", [script, "archive", "--html", plain, "--out", out], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  r = spawnSync("node", [script, "verify", "--dir", out, "--readback", plain], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(JSON.parse(r.stdout).ok, true);
});

const browser = findBrowser();
test("preview renders the plain template with no overflow", { skip: browser ? false : "no Chromium found" }, () => {
  const script = path.join(here, "preview-html-box.mjs");
  const shot = path.join(tmp, "plain.png");
  const r = spawnSync("node", [script, "--html", plain, "--screenshot", shot], { encoding: "utf8", timeout: 60000 });
  assert.equal(r.status, 0, r.stderr);
  const rep = JSON.parse(r.stdout);
  assert.deepEqual(rep.viewport, { width: 817, height: 560, scrollWidth: 817, scrollHeight: 560 });
  assert.deepEqual(rep.textOverflow, []);
  assert.ok(fs.statSync(shot).size > 1000);
});

test("preview fails on an overflowing page", { skip: browser ? false : "no Chromium found" }, () => {
  const bad = path.join(tmp, "bad.html");
  fs.writeFileSync(bad, good("", '<div style="width:2000px;height:20px">wide</div>').replace("overflow:hidden;", ""));
  const r = spawnSync("node", [path.join(here, "preview-html-box.mjs"), "--html", bad], { encoding: "utf8", timeout: 60000 });
  assert.notEqual(r.status, 0);
});
