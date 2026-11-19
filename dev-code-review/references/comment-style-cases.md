# Comment style cases

Calibration examples, not templates. The rule: match the user's language.

## Good inline comments

Architecture boundary (a framework depending on one plugin):

> The job runner now imports the email notifier directly. Sending a
> notification on completion is what the `on_finish` hook is for; wiring one
> notifier into the runner means every other notifier needs its own special
> case here. Suggest registering the email notifier through the hook.

Conflict check covering one direction only:

> This rejects a new booking that overlaps an existing one, but not the
> reverse: editing an existing booking so that it now overlaps a later one is
> accepted, and the calendar ends up double-booked. The overlap check needs to
> run on updates as well as inserts.

Lifecycle ordering bug:

> The cache entry is removed before the delete is committed. If the commit
> fails, readers miss on an item that still exists and reload a stale copy from
> the replica. Suggest evicting after the commit succeeds.

## Bad inline comments

Severity and location boilerplate the UI already supplies:

> [P1] `module.py:42` has a correctness problem that should be fixed before merge.

Vague risk with no trigger or effect:

> There may be some potential concurrency risks here, please consider this
> again.

Investigation diary and an oversized redesign in one line comment:

> I read the design doc, the call chain and the tests and ran three
> experiments. First, an overview of the whole framework, then five reasons
> this implementation is not ideal...

## Behavior cases

Positive:

- "Review this PR": bind the revisions, inspect and test, return concise
  findings, post nothing.
- "Post these comments": recheck the source revision, create the line
  comments, publish them, read them back, leave approval state alone.
- A reproduced failure: state it directly with only the trigger and the fix
  direction needed to act on it.

Negative:

- Do not post just because a review was requested.
- Do not publish a finding whose only evidence is a grep match or an empty
  reference result.
- Do not add severity labels, paths, or line numbers when concise inline
  wording is requested.
- Do not call a framework-boundary violation "just style" when it bypasses the
  extension model and forces generic code to know one plugin.
