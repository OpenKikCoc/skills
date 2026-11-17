# Publish and Archive

## Contents

1. Embed targets
2. Archive layout and manifest
3. Publish, read back, compare
4. Safe replacement
5. Extracting an existing box
6. Pitfalls

## 1. Embed targets

An embed target is wherever the HTML ends up being rendered. Anything works
as long as you can (a) write the HTML and (b) read the stored HTML back.

| Target | Write | Read back |
|---|---|---|
| Local file or static site | write the file | read the file |
| iframe `srcdoc` in a page you own | set the attribute in page source | read the page source and decode the attribute |
| Wiki, CMS, or document API record | API create/update | API get, then pull the HTML field |
| Slide or deck | bundle step | read the bundled output |

If a target cannot be read back, you cannot verify it. Say so, keep the
archive as the only proof, and ask a person to confirm visually.

## 2. Archive layout and manifest

```text
archive/<id>/
  index.html       exact source bytes
  manifest.json    title, sha256, bytes, target kind and id, preview file
  record.json      raw stored record, when extracted from one
  preview.png      verified screenshot (optional)
```

```bash
node scripts/archive-html-box.mjs archive --html box.html --out archive/<id> \
  --target-kind api-record --target-id <id> --preview box.png
```

`html_sha256` is over the exact UTF-8 bytes of `index.html`.

## 3. Publish, read back, compare

1. Validate and preview locally.
2. Archive the local file (the manifest hash is now the expected value).
3. Publish to the target.
4. Read the stored HTML back into a file. If it arrives inside a JSON record:
   `archive-html-box.mjs extract --json record.json --field result.html --out tmp/readback`
   (then use `tmp/readback/index.html`).
5. `node scripts/archive-html-box.mjs verify --dir archive/<id> --readback readback.html`
   exits 0 only on an exact byte match.
6. Also confirm the target-specific facts: the item exists exactly once, sits
   in the intended position, and carries the expected title.

Hash streams exactly: do not let a shell tool append a newline (`jq -j`, not
`jq -r`; `printf`, not `echo`). If the host normalizes HTML on save, define
and record the normalization, and compare normalized forms on both sides.

## 4. Safe replacement

```text
publish replacement -> read back -> hash, type, order, uniqueness checks
  -> delete old copy -> final read of the surrounding page
```

Never delete first. If the host rejects in-place edits of a stored record,
treat every edit as a replacement. Do not reuse stale revision ids or
positional indexes after structural writes; refetch them. Do not run
dependent writes to the same page in parallel. Preserve unrelated edits and
comments on the page.

## 5. Extracting an existing box

Fetch the raw stored record (not a rendered placeholder), save it as JSON,
then `extract --json record.json --field <dotted.path> --out archive/<id>`.
The field may hold the HTML directly or a JSON string or object with an
`html` key. The raw record is kept as `record.json`.

## 6. Pitfalls

- Rendered or exported views may show only a placeholder for the box; fetch
  the raw record for source.
- Do not substitute a PNG when the request is an interactive box.
- Double-escaped attributes: decode input entities, escape once on output.
- Keep the archive free of credentials, personal data, and preview stubs.
