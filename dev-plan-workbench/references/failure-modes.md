# Failure modes

| Symptom | Discriminator | Prevention |
|---|---|---|
| Writing before understanding; wrong owner, layer, or scenario | Can each component be defined in one line with layer, scope, source? | Concept cards; read actual designs; scope/layer lane |
| Instruction keywords echoed literally; shallow "adversarial" review | Is the instruction visible only as a slogan? Did reviewers try to falsify? | Translate into effects; `banned_terms`; independent lanes |
| Invented target, placeholder, half-invented mechanism, private-note source | Does each claim have a permitted source or an explicit proposal basis? | Three dispositions; placeholder lint; NUMBERS and note-permission gate |
| Comparison uses excluded pools or an incompatible denominator | Can numerator, exclusions, window, unit reproduce it? | NUMBERS basis; data-adversarial recompute; conservative bounds |
| Optional enhancement described as a blocker | Prerequisite, or improvement? | Facts lane separates merged, rolling out, proposed, non-blocking |
| Wrong voice; coined jargon; lost owner terms | Is the term source-defined and preserved? | CONTRACT voice; first-person and banned-term lint; reader lane |
| Flat or repetitive structure; figures precede logic or contradict text | Does each section add a judgement and each figure an argument? | Accepted skeleton; logic before figures; final integrated read |
| Many point patches, no convergence | Were all findings ruled on before editing? | TRIAGE all findings; one consolidated pass; gate on the diff |
| Rules lost after compaction | Is `next_action` recoverable without transcript memory? | Durable state; recovery order; immutable versions |
| Live revision moves or collaborators edit | Is live content equal to the approved base? | Revision guards; canonical diff; re-plan on equal drift; block on changed drift but still deliver the local version |
| Downstream write overwrites someone's work | Are target blocks unchanged from the baseline, with fresh approval? | Subset declaration; scoped approval; targeted edits; stop on conflict |
| Delete before verify loses content | Do hashes and order agree before deletion? | Insert, read back, then delete, then verify again |
| Resume ledger reused for a different intent; timeout duplicates a write | Does the ledger bind document, manifest, plan, verified prefix? | Bound ledgers; re-read completed steps; no blind retry |
| Names or private chat leaked to a worker; confident lane overclaims | Is context sanitized and each result reproducible? | Minimal read-only context; private FEEDBACK; coordinator spot-checks |
| Scan aborts on grep no-match under `set -e` | Is "no match" treated as success? | Use standard-library scanning or handle the status explicitly |

Audit every row before publication. Lint catches lexical issues only; scope,
permission, provenance, and reader logic need the named manual gates.
