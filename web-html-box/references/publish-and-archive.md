# Publish and Archive

## Contents

1. Embed targets
2. Archive layout and manifest
3. Publish, read back, compare
4. Safe replacement
5. Outcome unknown
6. Extracting an existing box and the archive envelope
7. Pitfalls

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
pin old box hash -> publish replacement -> read back -> hash, type, order,
  uniqueness checks -> whole-page comparison -> delete old copy
  -> whole-page comparison -> final read of the surrounding page
```

Never delete first. If the host rejects in-place edits of a stored record,
treat every edit as a replacement. Do not reuse stale revision ids or
positional indexes after structural writes; refetch them. Do not run
dependent writes to the same page in parallel.

Procedure:

1. **Pin the old box.** Archive the approved old source and compare the live
   old box against it before any write:
   `archive-html-box.mjs extract --json record.json --field <path> --out archive/<old-id> --expect-old-sha256 <approved-hash>`.
   The hash comes from the approved old source, not from whatever the latest
   fetch returned. A mismatch means someone changed the box: do not insert and
   do not delete; re-plan.
2. **Record the approved baseline** of the whole page: every block's content
   and order, plus the current revision.
3. **After each publish and after each delete**, re-read the page and compare
   all non-target content and its order against the approved baseline plus the
   one intended change. Any difference stops the procedure; a failed delete
   check means the old box may already be gone, so reconcile before anything
   else.
4. **Comments.** List comments before and after, paging until the provider
   reports no more pages (and replies likewise). State the scope you listed
   (for example unresolved only); a default scope is not a claim that resolved
   history was preserved. Compare content, replies, resolution state, and
   anchors by hand: block equality does not cover comments.
5. Preserve unrelated edits and comments on the page.

## 5. Outcome unknown

A write whose result you did not observe is **`outcome_unknown`**: a timeout,
a missing response, a malformed or truncated response, a dropped connection,
or an identity check that fails after the attempt. It is not evidence of
failure and not evidence of absence.

- Keep the intent, the attempted target, the revision you wrote against, and
  every partial artifact.
- Re-read the exact target with the same identity and the same query before
  doing anything else. Never blind-retry, delete, or move on to the next write.
- An idempotency or client token is not retry-safe unless the provider
  guarantees same-key replay for the same operation. Do not infer that from a
  library; rotating the token on retry is a duplicate write.
- After re-reading: exactly one match for the intended content, position and
  hash is a late success (record it as applied); zero matches is absence, which
  alone is still not permission to replay; multiple or conflicting copies mean
  preserve all of them and stop for a person.
- A fresh page revision does not authorize a retry. It only tells you what
  state to compare against.
- Rollback is an operator action, never automatic. Use it only after proving
  that no later edit by someone else would be lost, and with explicit approval
  of the target version.

The generic state machine is in
`dev-iteration-loop/references/external-write-transaction-contract.md`; use it
rather than inventing another one.

## 6. Extracting an existing box and the archive envelope

Fetch the raw stored record (not a rendered placeholder), save it as JSON,
then `extract --json record.json --field <dotted.path> --out archive/<id>`.
The field may hold the HTML directly or a JSON string or object with an
`html` key. The raw record is kept as `record.json`.

Archive envelope rules, for archiving a whole page or document with several
boxes:

- Keep the **entire fetch response** as the source of record; never reduce it
  to a rendered view or to the HTML you happened to extract.
- Each placeholder in the stored document maps to exactly one local box
  archive and each box archive to exactly one placeholder (1:1 coverage).
  Count both directions; an unmapped placeholder or an orphan archive fails.
- Reject, rather than repair, any source reference that is absolute, escapes
  the archive root with `..`, passes through a symlink, points at a remote
  URL, or resolves to an empty file. `archive-html-box.mjs` itself refuses
  empty, symlink, and non-regular `--html` sources.
- Resolve every path against the skill root for tools and against a private
  workspace directory outside the skill for data (see SKILL.md Paths).

## 7. Pitfalls

- Rendered or exported views may show only a placeholder for the box; fetch
  the raw record for source.
- Do not substitute a PNG when the request is an interactive box.
- Double-escaped attributes: decode input entities, escape once on output.
- Keep the archive free of credentials, personal data, and preview stubs.
