# Publishing (target-agnostic)

Applies to any destination: a wiki, a document editor, a repository, a page.

1. **Approval.** Get fresh approval: explicit, scoped to the exact target, section, and
   reviewed bytes. A reviewer verdict or passing lint is not approval.
   Downstream writes to a section other people maintain need their own
   fresh approval; compare current target content to the last-written (or approved)
   baseline and stop if anyone changed it.
2. **Back up first.** Save live content, the reviewed version, source hashes,
   and a manifest under `publish/`.
3. **Plan against a revision.** The plan names the live revision it was
   computed from. Check the revision before every mutation; never overwrite the
   whole document; preserve untouched blocks, comments, and order.
4. **Apply, then read back.** Verify position, content hash, and that
   unrelated content and comment threads are unchanged. List comments before
   and after (all pages).
5. **Unknown outcome.** A timeout or malformed response after a write is
   `outcome_unknown`: re-read the exact target before anything else; accept only
   an exact-match late success; no blind retry; no automatic rollback. If the
   content is equal but the revision drifted, re-plan from the new revision and
   never waive the guard. See
   `dev-iteration-loop/references/external-write-transaction-contract.md`.
6. **Embedded HTML.** Replace procedure and hash checks are in
   `web-html-box/references/publish-and-archive.md`; figures come from
   `web-diagram`.
7. **Ledger.** Record the live revision, verification results, and ledgers in
   HANDOFF. Distinguish offline from live verification.
