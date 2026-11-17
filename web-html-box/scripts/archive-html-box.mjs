#!/usr/bin/env node
// Archive an HTML box with a sha256 manifest and verify a read-back copy.
// Node >= 18, no dependencies.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

function usage() {
  console.log(`Usage:
  archive-html-box.mjs archive --html <file> --out <dir> [--target-kind K] [--target-id ID] [--preview png]
  archive-html-box.mjs extract --json <file> --field <dotted.path> --out <dir> [--target-kind K] [--target-id ID] [--preview png]
  archive-html-box.mjs verify  --dir <archive-dir> --readback <file>

archive  Store a local HTML file as index.html plus manifest.json.
extract  Pull the HTML string out of a stored record (JSON file) first. The
         field may hold the HTML directly or a JSON string/object that itself
         has an "html" key. The raw record is kept as record.json.
verify   Compare sha256 of a read-back copy with the manifest. Exit 0 only on
         an exact byte match; only then delete the old copy.
`);
}

export const sha256 = (value) => crypto.createHash("sha256").update(value).digest("hex");

export function titleFromHtml(html) {
  const m = html.match(/<title>([\s\S]*?)<\/title>/i);
  if (!m || !m[1].trim()) throw new Error("HTML must contain a non-empty <title>");
  return m[1].trim().replace(/\s+/g, " ");
}

export function getPath(obj, dotted) {
  let cur = obj;
  for (const key of dotted.split(".")) {
    if (cur === null || typeof cur !== "object" || !(key in cur)) {
      throw new Error(`field not found: ${dotted}`);
    }
    cur = cur[key];
  }
  return cur;
}

export function htmlFromRecord(json, field) {
  let value = getPath(json, field);
  let record = null;
  if (typeof value === "string" && /^\s*[{[]/.test(value)) {
    try { value = JSON.parse(value); } catch { /* treat as html text */ }
  }
  if (value && typeof value === "object") {
    record = value;
    value = value.html;
  }
  if (typeof value !== "string" || !/<html[\s>]/i.test(value)) {
    throw new Error("field does not contain standalone HTML");
  }
  return { html: value, record };
}

export function buildManifest(html, meta = {}) {
  return {
    schema_version: 1,
    title: titleFromHtml(html),
    html_file: "index.html",
    html_sha256: sha256(html),
    html_bytes: Buffer.byteLength(html),
    record_file: meta.record ? "record.json" : null,
    preview_file: meta.preview || null,
    target_kind: meta.targetKind || null,
    target_id: meta.targetId || null,
  };
}

export function writeArchive(outDir, html, meta = {}) {
  const dir = path.resolve(outDir);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "index.html"), html);
  if (meta.record) {
    fs.writeFileSync(path.join(dir, "record.json"), `${JSON.stringify(meta.record, null, 2)}\n`);
  }
  let preview = null;
  if (meta.previewSource) {
    preview = "preview.png";
    fs.copyFileSync(path.resolve(meta.previewSource), path.join(dir, preview));
  }
  const manifest = buildManifest(html, { ...meta, preview });
  fs.writeFileSync(path.join(dir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  return manifest;
}

export function verifyReadback(dir, readbackFile) {
  const root = path.resolve(dir);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
  const stored = fs.readFileSync(path.join(root, manifest.html_file));
  const back = fs.readFileSync(path.resolve(readbackFile));
  const storedHash = sha256(stored);
  const backHash = sha256(back);
  return {
    ok: storedHash === manifest.html_sha256 && backHash === manifest.html_sha256,
    manifest_sha256: manifest.html_sha256,
    archive_sha256: storedHash,
    readback_sha256: backHash,
  };
}

function parseArgs(argv) {
  const [cmd, ...rest] = argv;
  const args = { cmd };
  const keys = {
    "--html": "html", "--out": "out", "--json": "json", "--field": "field",
    "--dir": "dir", "--readback": "readback", "--preview": "preview",
    "--target-kind": "targetKind", "--target-id": "targetId",
  };
  for (let i = 0; i < rest.length; i += 1) {
    const key = keys[rest[i]];
    if (!key) throw new Error(`Unknown argument: ${rest[i]}`);
    if (i + 1 >= rest.length) throw new Error(`${rest[i]} requires a value`);
    args[key] = rest[++i];
  }
  return args;
}

function main() {
  const argv = process.argv.slice(2);
  if (!argv.length || argv[0] === "-h" || argv[0] === "--help") return usage();
  const args = parseArgs(argv);
  const meta = { targetKind: args.targetKind, targetId: args.targetId, previewSource: args.preview };
  let manifest;
  if (args.cmd === "archive") {
    if (!args.html || !args.out) throw new Error("--html and --out are required");
    manifest = writeArchive(args.out, fs.readFileSync(path.resolve(args.html), "utf8"), meta);
  } else if (args.cmd === "extract") {
    if (!args.json || !args.field || !args.out) throw new Error("--json, --field and --out are required");
    const { html, record } = htmlFromRecord(JSON.parse(fs.readFileSync(path.resolve(args.json), "utf8")), args.field);
    manifest = writeArchive(args.out, html, { ...meta, record });
  } else if (args.cmd === "verify") {
    if (!args.dir || !args.readback) throw new Error("--dir and --readback are required");
    const result = verifyReadback(args.dir, args.readback);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    if (!result.ok) process.exitCode = 1;
    return;
  } else {
    throw new Error(`unknown command: ${args.cmd}`);
  }
  process.stdout.write(`${JSON.stringify(manifest, null, 2)}\n`);
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  try {
    main();
  } catch (error) {
    console.error(`archive-html-box: ${error.message}`);
    process.exitCode = 1;
  }
}
