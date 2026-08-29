# The review team

Seven agents, each encoding a technique that has actually found a defect in
this codebase. They are not generic roles — there is no "code reviewer" here,
because a generic reviewer reads the diff and a generic reviewer is what
already passed all 855 tests.

Every one of them shares a single instruction, and it is the one that matters:
**probe, don't reason.** Run it as the production role. Look at the rendered
page. Generate 100× the rows and time it. Set the clock to the hour that
breaks it. A hypothesis you have not made fail is not a finding.

| Agent | Runs when | What it caught |
|---|---|---|
| `blueprint-fidelity` | **Before** substantial work | Scope drift toward table stakes |
| `invariant-auditor` | Reviewing a module | A no_show correction erasing the guarantee record its own docstring protected |
| `production-role-verifier` | After any table, view, route or write | Four privilege gaps invisible to the whole suite |
| `data-rights-auditor` | After a migration adds a column | Erasure leaving the person's name in message bodies |
| `screen-reviewer` | After a template change | Net pay correct in SQL, absent from the page |
| `time-and-order-sentinel` | After date or schedule logic | Five tests that failed only at 01:09 Kigali |
| `scale-prober` | Before trusting a list or report | Measured, not imagined |

## Order

`blueprint-fidelity` runs **first**, before code — a constraint conflict found
after a week of work is a week of work. The rest run after, and
`production-role-verifier` is the one never to skip: the test suite connects as
the database owner and bypasses every grant, so it is structurally blind to
the class of bug that breaks the first real deployment.

## Reporting

A clean audit is a result. Say "no defects found, here is what I ran" — that is
more useful than a manufactured finding, and this project has already deleted
one test that enforced a style preference dressed up as a property.
