# Read-Only Disk Audit

Do not start repeated pressure checks with a whole-home `du`, and do not rescan
the same temporary root separately for totals, grouping, and Top N. Use one
stable bounded scope. On macOS, for example:

```bash
python3 <skill-root>/scripts/quick_disk_audit.py \
  --path "$HOME" \
  --scan-root /private/tmp \
  --group 'project-a=^project-a-' \
  --group 'project-b=^project-b-' \
  --focus /System/Volumes/VM \
  --jobs 4 --top 20 \
  --output <state-root>/disk-audit-fast.json
```

For a later identical sample:

```bash
python3 <skill-root>/scripts/quick_disk_audit.py \
  <same-scope-arguments> \
  --previous <state-root>/disk-audit-fast.json \
  --output <state-root>/disk-audit-fast-next.json
```

The audit measures each discovered top-level child/focus at most once and
accepts a baseline only when its internal scope hash is valid and matches the
new request. Never compare fast and deep scopes as one time series.

If the filesystem delta remains unexplained, start a separately baselined deep
scope over reviewed paths (on macOS, for example):

```bash
python3 <skill-root>/scripts/quick_disk_audit.py \
  --path "$HOME" \
  --focus "$HOME/Library/Caches/go-build" \
  --focus "<agent-state-dir>" \
  --focus "$HOME/src" \
  --focus "$HOME/go/pkg/mod" \
  --focus "${TMPDIR%/}" \
  --jobs 4 --top 20 \
  --output <state-root>/disk-audit-deep.json
```

`--cleanup-candidates` classifies the already measured Top N without another
size scan. It always reports `cleanup_authorized=false` and unknown reclaimable
bytes. A marker only selects the next ownership/activity check; it is not
deletion authority.

Use exact-path `lsof`, deeper `du`, product-native inspection, or APFS tools
only for a reviewed Top N item, material delta, or unexplained filesystem
change. Inconclusive activity blocks cleanup. Session history needs
product-aware archive and restoration checks; never move live databases or
delete raw history merely because it is old.
