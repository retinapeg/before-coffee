# Before Coffee

**Explainable selection with deterministic signals, and a measured decision about when not
to use machine learning.** Each morning the system picks a short list from a pool of
items, orders it, and attaches a plain reason to every item: why it was chosen. Nothing
in the selection is learned, and that is deliberate. The centrepiece of this repository
is the analysis behind that choice: a contextual bandit was specified, the data it would
learn from was measured, and the model was not built, because the data could not support
an honest evaluation. The worked example is job vacancies: a daily plain-text digest,
sent at 07:00 to its owner's own mailbox, of roles collected from employer
applicant-tracking boards.

- **Runs offline:** 79 tests and a synthetic demo on a clean clone, with nothing but
  `pytest` installed. See [Run it offline](#run-it-offline).
- **The decision record:** [Why there is no bandit yet](#why-there-is-no-bandit-yet).

![Architecture of Before Coffee: an opt-in hourly refresh loop starts or resumes the private collector, which writes vacancies into a SQLite store; at 07:00 digest_cli selects roles with fixed rules, a location classifier and three signals, then renders a plain-text email and sends it through Gmail to the owner's own inbox, recording the sent ids; stand-ins and the offline demo are shown dashed](docs/images/architecture.svg)

*Purple: model call · blue: deterministic code · green: human · amber: evaluation · grey: storage · dashed: external, optional, mocked or planned*

## Where AI sits, and where it does not

No code in this repository calls a model. Selection is a filter followed by a fixed
order. Each item carries a reason from three deterministic signals ([How selection
works](#how-selection-works)). The fit band that the filter reads is computed upstream by
the private system and is only read here.

The machine-learning work is the decision in the next section: a model-iteration choice
made from measured data, including the choice not to fit anything yet.

## Why there is no bandit yet

**The question.** Should the digest learn from how its reader responds, using a
contextual bandit that chooses which items to show?

**What was measured.** These figures come from the private job store on 21 September
2026, which is not published, so they cannot be reproduced from this repository.

- **The labels are positive-unlabelled.** There were 215 historical positives (roles
  marked "saved") and **zero** explicit negatives. The other ~2,600 roles are unlabelled,
  not rejected. Treating them as negatives would bias a model towards whatever the old
  interface happened to show.
- **The history cannot be evaluated off-policy.** Those saves were collected under a
  policy that never recorded propensities, so no inverse-propensity estimate built on
  them is honest.
- **A reward read from job status would have been confounded by source.** This is the
  number that settled it:

  | Source of role | Qualifying | Already acted on |
  |---|---:|---:|
  | Legacy import | 80 | 77 (**96%**) |
  | Live employer boards | 258 | 4 (**2%**) |

  A learner rewarded on status would have latched onto a stale legacy import and written
  off live listings, and every offline metric would have said it was improving.
- **There is almost nothing to allocate.** On the one full night measured, 18 new roles
  qualified against a daily cap of 25. Once the backlog clears, every qualifying role goes
  out the day it arrives, so a bandit would be choosing from a single option.

**The decision.** Fit nothing yet. First build the measurement layer:

- log every impression with its propensity, and with its features *as they were at send
  time*;
- collect labels from replies to the digest, so no tracking is needed;
- keep a fixed control policy for comparison, so it is possible to tell whether learning
  helps at all.

**What would change it.** Two conditions together: labels that come from the digest
itself rather than a legacy import, and a daily cap that actually binds, so that there is
a choice to learn. Neither holds yet, and the measurement layer is not built yet.

## Run it offline

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
python scripts/demo.py
```

The tests need only `pytest`, and the demo needs only the standard library. The demo seeds
a temporary store with invented vacancies, runs the real ingestion census, then runs the
real digest selection and rendering as a dry run. It never contacts Gmail. Excerpt from a
real run:

```
SYNTHETIC DEMO - every employer, role and link below is invented. Nothing is sent and Gmail is not contacted.

considered 7 | qualifying 5 | selected 5 | held back 0 | in an earlier digest 0 | below the criteria 1 | outside configured locations 1

LONDON
------

  Data Engineer
    Example Grid Ltd (synthetic)  |  London, United Kingdom
    Salary: GBP 55,000 to 65,000
    posted 3 hours ago
    Why: Meets your criteria and the employer published it in the last 24 hours.
    https://jobs.example.invalid/1

  Carbon Accounting Analyst
    Sample Ledger Co (synthetic)  |  London
    Salary: not published
    posted 30 hours ago
    Why: Meets your criteria, and the title is not one your configured searches look for.
    https://jobs.example.invalid/2
```

## How selection works

1. Skip anything in the ledger of items already sent.
2. Keep items whose fit band is strong or plausible and whose location is London or a
   configured country.
3. Order by the employer's own posting date, newest first; items without one come last.
4. Take the first 25. Anything held back stays eligible for the next digest.
5. Print two sections, **London** then **International**, with a reason under every item.

Each item's reason comes from three deterministic signals:

| Signal | Fires when |
|---|---|
| **Source** | the item is on the employer's own board rather than an aggregator |
| **Fresh** | the employer's own posting date falls inside the window |
| **Adjacent** | the item meets the criteria, but its title is not one the configured searches would have found |

*Adjacent* is what produces "a role you would otherwise have missed". The date an item
was first *seen* is never treated as its posting date. On the private store, fewer than
half the qualifying roles carried a posting date at all, so *Fresh* rarely fires.

The email never shows a percentage, a score or a claim about anyone's chances, and it
contains no tracking pixel. Links are printed exactly as the source published them.

## What the tests check

The 79 tests use invented data only. Tests written against real personal data end up
being tests about a person, and they would leak that data into the repository. They check
properties rather than examples:

- the email never contains a percentage, a score or a claim about someone's chances;
- a hostile item title cannot inject headers or control characters;
- a real send goes only to the mailbox Gmail confirms as authenticated, unless another
  recipient has been explicitly enabled;
- an unusable token raises an error and never opens a browser consent flow;
- an item held back by the cap turns up in the next digest;
- a scheduled collection run can widen coverage but never narrow it;
- a crashed worker cannot block polling forever, and a live run is never interrupted;
- the demo runs end to end, its counts add up, and it opens no network connection.

![Output of python -m pytest -v on a clean clone: all 79 tests pass, and their names read as the properties listed above](docs/images/signals-tests.png)

*Rendered from the raw output of a real clean-clone run of `python -m pytest -v`
([text](docs/images/signals-tests.txt)).*

`scripts/digest_ingestion_census.py` counts what collection has stored and exits
non-zero if any count falls between two snapshots.

## Engineering findings

These came up while the system ran on the private store. The figures are from that store
and cannot be reproduced here.

**A collection bug that every metric reported as healthy.** The hourly collector
restarted from scratch on each run. Because it always visited boards in the same order,
every run hit its time limit in the same place. It reached 5 of 62 employer boards before
stopping at 180 seconds and dropped a queue of 256 more. Two London runs contacted exactly
the same eight hosts, and the second stored zero new roles. Stored counts never fell, so
nothing looked wrong. The fix resumes each run from the previous checkpoint. Because a
resumed run inherits its elapsed time, its budget grows along the chain as
`consumed + slice`, and every limit is written with `max()` so a scheduled run can widen
coverage but never narrow it. After the fix, new roles per run went from 0 to 517, 843 and
1,154.

**A clean-up step that never ran.** The collector stopped polling for 55 minutes while its
own "is a poll due?" check kept answering yes. A worker cleared its "running" flag in a
`finally` block whose first statement raised `database is locked`, so the flag was never
cleared. The loop now clears only flags whose stored run status is positively terminal. An
unknown run is left alone, because two runs over one database would do more harm than a
missed poll.

**Scores that rewrite themselves.** Opening the private job store recalculates every fit
score. So a score read today is not the score the reader saw last week, and any impression
log has to store features as they were at send time. The stand-in store in this
repository deliberately does not rescore.

**A filter that would have lost data.** Selecting "items first seen since the last
digest" silently drops everything a daily cap held back, because those items were first
seen before that digest. A ledger of sent IDs replaced the time filter.

**A permission that could widen itself, found and closed.** A digest may go only to its
owner's own mailbox. While this repository was being made to run offline, one path was
found to fail open: if the Gmail profile lookup failed and the token file named no
account, a configured address that was not the authenticated mailbox would receive the
email. A real send now requires Gmail to confirm the mailbox, unless another recipient has
been explicitly enabled. Regression tests cover both cases.

## What is and isn't here

This code was extracted from a larger private system. The collector and the web app that
starts it are not included. Where the digest depends on private modules, this repository
carries **minimal stand-ins** so that everything here runs. Each stand-in says in its
docstring how it differs from the private version.

| Path | What it is |
|---|---|
| `src/careerops/digest.py` | selection, ordering, sections and the plain-text email |
| `src/careerops/signals.py` | the three signals |
| `src/careerops/digest_delivery.py` | self-only delivery and the recipient guard |
| `src/careerops/digest_cli.py` | the entry point the 07:00 schedule runs |
| `src/careerops/refresh.py` | the hourly collection loop, continuation chain and flag clean-up |
| `src/careerops/store.py` | stand-in: a minimal SQLite store with the same tables; it does not rescore on open |
| `src/careerops/inventory.py` | stand-in: a location classifier that matches the word London and configured city names only |
| `src/careerops/discovery.py` | stand-in: coverage limits and configured search queries |
| `src/job_cv_agent/` | Gmail helpers: the send function, plus a token helper that refreshes and never starts a consent flow |
| `scripts/demo.py` | the offline demo |
| `scripts/digest_ingestion_census.py` | shows that collection coverage never shrinks |
| `scripts/install_digest_schedule.sh` | installs the 07:00 job as a macOS LaunchAgent |
| `scripts/reauthorise_gmail.py` | restores Gmail access when a token has expired (needs the Google client libraries) |

## Status

Running daily on the private store since 21 September 2026. Next is the measurement layer
from the decision above: impression and propensity logging, and reply-based labels. A
learner comes after that, and only once the labels can support an honest evaluation.
