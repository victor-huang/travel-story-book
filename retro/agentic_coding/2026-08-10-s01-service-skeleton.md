# S01 — service skeleton, deployment shape, CI

Two entries. Both are about the same thing from opposite ends: what a "healthy" service is allowed
to claim, and what a task is allowed to decide on the human's behalf.

### A health endpoint returned 200 for a service that could not do its one job

**Cycle:** Wave S / S01
**Cost:** ~5 min, caught on the first real run
**Category:** near-miss

**Symptom.** The service was started with `./.venv/bin/uvicorn storybook_service.app:app`. `/health`
answered `200 {"status":"ok"}`. `/ready` answered `503`:

```
story_book_cli  false  "story-book --version: [Errno 2] No such file or directory: 'story-book'"
```

**Root cause.** Invoking a venv binary by path does not put `.venv/bin` on `PATH`. The `story-book`
executable — the entire reason this service exists — was unresolvable, and every ASGI-level check
was nonetheless perfectly green. The false belief was mine and it was in the *launch command*, not
the code; the code was right and said so.

**Fix.** Nothing to fix in the product. `service/README.md` now says to launch through `uv run`, and
quotes the exact `503` body, because that is the error a reader will actually see. The Dockerfile
was already immune (`PATH` includes `/opt/venv/bin`), which is why CI would not have caught it.

**Lesson.** This is `P06` again in another costume: 87 tests asserted *presence* while nine files
were the wrong type. A liveness check asserts presence of a process. Only a check that names the
dependency and reports what it measured can tell a working deployment from a running one — and it
has to be a separate endpoint, because a platform that restarts on a failed readiness probe loops
forever on a broken image instead of reporting one. Encoded as a test with a control: the real
probe must find the CLI, *and* a deliberately wrong binary name must be reported as a failure with
a reason attached, so `ok: true` cannot mean "the probe can only return true".

### A control test caught a seam I had built and would not have noticed

**Cycle:** Wave S / S01
**Cost:** ~5 min
**Category:** near-miss

**Symptom.** `create_app(settings)` stored settings on `app.state`, and the FastAPI `lifespan`
handler then overwrote them with `Settings.from_env()` before probing. Any caller-supplied settings
were silently discarded.

**Root cause.** Two places initialise `app.state.settings` and the later one won. Noticed while
writing the control test that points the probe at a nonexistent binary — that test would have
failed, reporting "the wrong binary was found", which is a confusing way to learn about a
constructor bug.

**Fix.** `lifespan` prefers settings pinned before startup and falls back to the environment.

**Lesson.** The control test earned its keep before it ran once. A test whose only job is to make
the positive case falsifiable also documents the seam the positive case travels through, and that is
where the bug was.

### What was deliberately not decided, and why that is the deliverable

**Category:** design-flaw (in the plan, found by trying to implement it)

S01's own entry said it "needs the human's ratification before code". The language half had a strong
recommendation in open question 13; the hosting half had nothing. Building an object-store client or
a queue would have committed S02–S07 to a hosting target chosen by an agent on no information, and
each of those tasks would then have inherited it as a fact.

So the skeleton is one container image with two endpoints and no adapters, and five questions were
written down instead of answered: hosting (14), object store (15), queue (16), the multi-trip index
(17), and whether the image ships `clip` (18).

**Question 17 is a hole in the design docs rather than an ambiguity.** D8 says "the service indexes
trips and reels". `story.db` is one SQLite file *per trip* with a single-row `trip` table enforced by
`CHECK (id = 1)`, so it cannot hold users, a trip list, jobs or reels — and no design doc names
anything that can. Both S06's account-deletion problem (question 3) and question 4's per-trip versus
per-user storage are answered against whatever that store turns out to be, so it is upstream of two
questions that already looked blocking.

**Lesson.** When a task's own entry says a decision needs the human, the useful output is a
decomposed set of choices with their consequences, not an implementation that hides one of them.
The cheap enforcement here is the one already in the tracker — "escalate to the human, don't
improvise" — and what this cycle adds is that the escalation should name *which later tasks inherit
the guess*, because that is the argument for stopping.
