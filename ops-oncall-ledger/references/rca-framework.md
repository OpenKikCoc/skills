# RCA framework

Separate observed facts, hypotheses, falsifiers, and recovery evidence. The
rendered `rca/<id>.md` has these sections:

1. **Executive summary and current status** - what broke, diagnosis state, and
   recovery plan/execution/verification stated independently.
2. **Ticket identity and scope** - priority, date, category, environment, source.
3. **Impact and failure boundary** - symptom, confirmed scope, first failed
   stage, stages not entered.
4. **Timeline** - key transitions only, each with evidence.
5. **Root cause analysis** - state, mechanism, evidence refs. Use as many causal
   steps as evidence supports; Five Whys is a technique, not a quota. An unknown
   step names the evidence gap and the next discriminating check.
6. **Evidence chain and hypotheses** - facts, rejected and open hypotheses with
   falsifiers, the evidence catalog.
7. **Remediation and responsibility** - actions with owner and status.
8. **Recovery acceptance and open questions** - execution is not verification;
   only a passed post-action observation verifies.
9. **AI assistance** - agent used, accuracy, basis, material corrections.

Method: write several hypotheses first, pick the check that best separates two
of them, record the result whether it confirms or rejects, and never promote a
retry symptom or trigger to root cause. Do not overwrite a human-authored RCA
wholesale; add or replace only the rendered sections and keep citations.
