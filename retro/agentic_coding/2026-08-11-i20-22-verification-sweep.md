# I20/I21/I22 — verifying uncommitted work left mid-run

One entry: a full-suite failure that was the harness, not the code, and the reason it took a
second run rather than a glance to tell the difference.

### A backgrounded `uv run pytest -q` reported four SIGINT-test failures that a foreground run did not

**Cycle:** Wave 2 / I20–I22 (verification sweep, not new code)
**Cost:** ~10 min, one extra full-suite run
**Category:** environment

**Symptom.** `cd /Users/victor.huang/work/travel-story-book && uv run pytest -q`, launched through
the `run_in_background` path because the sweep's 120 s foreground timeout was too short for the
full suite, came back with:

```
FAILED tests/backend/test_runner.py::TestResumeAfterInterrupt::test_interrupt_is_reported
FAILED tests/backend/test_runner.py::TestResumeAfterInterrupt::test_work_completed_before_the_interrupt_is_saved
FAILED tests/backend/test_runner.py::TestResumeAfterInterrupt::test_resume_recomputes_only_unfinished_items
FAILED tests/backend/test_runner.py::TestResumeAfterInterrupt::test_later_stages_are_not_started_after_an_interrupt
```

with a wrong-seeming `completed_hashes(...) == {'__trip__'}` assertion. Nothing in this session
touched `pipeline/runner.py`, `db/`, or anything else under `src/story_book/` — the uncommitted
work is entirely under `ios/`.

**Root cause.** `TestResumeAfterInterrupt` fires a real `SIGINT` at itself (CLAUDE.md already names
this as the highest-value failure-mode test in the suite: "a test of a failure mode must be shown
to fail"). Running the whole suite through this session's `run_in_background` tool — a
non-interactive backgrounded shell — is exactly the shape T43's own retro already warned about:
signal delivery is not guaranteed to land the same way there as in a foreground terminal. Rerunning
the identical command, unbackgrounded, with output redirected to a file, passed clean: 1772 tests,
exit 0. Rerunning `test_runner.py` alone also passed clean, twice.

**Fix.** Nothing in the product. The sweep now treats "backgrounded run failed a SIGINT test" as
grounds for an immediate foreground rerun before concluding anything, rather than as a finding to
report or investigate in the code.

**Lesson.** T43's lesson ("a non-interactive shell's background jobs ignore \[SIGINT\]") is not
confined to the test that discovered it — it is a property of *how this session invokes commands*,
and it recurs every time a large test run gets pushed to `run_in_background` to dodge a timeout.
The cheap fix is procedural: any red result from `TestResumeAfterInterrupt` (or anything else that
sends itself a signal) earns one free foreground rerun before it is treated as evidence, because
the backgrounding mechanism is a plausible cause the test's own subject matter cannot distinguish
itself from.
