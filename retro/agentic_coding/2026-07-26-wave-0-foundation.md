# Wave 0 — foundation (2026-07-26)

Built T01–T06: repo skeleton, config, DB schema, stage framework, fixtures, CLI. Ended at 186
tests passing, CI green on macOS + Linux, pushed public.

Eight entries. Three were wrong tests or wrong assumptions on my part, two were environment
surprises, one was a genuine design flaw that reached CI, one a near-miss caught before it
became permanent, and one a bug caught by reading rather than by tests.

---

### Two stages with different names were expected to share a resume cache

**Cycle:** Wave 0 / T04
**Cost:** ~5 min, 1 red local run
**Category:** wrong-test

**Symptom.** `test_a_failed_item_is_retried_on_the_next_run` expected the rerun to recompute
exactly `["item005"]` (the one item that had failed). It recomputed all 100.

**Root cause.** The test ran `FailingStage` (`name = "failing"`) and then `CountingStage`
(`name = "counting"`). The cache key is `(media_hash, stage_name, stage_version)`, so those are
two independent stages with independent caches — the second one had no cached results at all
and correctly recomputed everything. The code was right; my test encoded a false belief that
the two classes shared an identity because one subclassed the other.

**Fix.** Gave `FailingStage` the same `name` as `CountingStage`, with a comment saying why the
pairing exists. `36b10a8`'s predecessor, in `8e70728`.

**Lesson.** **A stage's cache identity is its `name` string, not its class.** Subclassing does
not inherit cache identity — and that's deliberate, since a subclass is usually a different
computation. Any test about resume behavior must be explicit about which stage name it's
exercising.

---

### Video fixture tests gated on the binary instead of the artifact

**Cycle:** Wave 0 / T05
**Cost:** ~10 min, 1 red CI run, and it was the first-ever CI run on a fresh public repo
**Category:** design-flaw

**Symptom.** First CI run failed on both runners: `assert (media_dir / "clip_speech.mov").exists()`
→ `AssertionError: assert False`, with `has_ffmpeg = True` in the traceback.

**Root cause.** The tests skipped when `ffmpeg` was absent, on the assumption that no ffmpeg
implied no fixtures. Those two conditions are independent. CI installs ffmpeg via brew/apt and
*then* checks out a tree whose video fixtures hadn't been committed yet — so `has_ffmpeg` was
`True`, the skip didn't fire, and a case intended to skip became a hard failure. I had even
predicted the wrong outcome out loud beforehand ("the two video tests will still skip there"),
which is worse than not predicting at all.

**Fix.** Fixtures are committed artifacts exactly like the images, so their presence is now
asserted unconditionally; only the ffprobe-based checks depend on the binary and carry
`needs_ffmpeg`. Verified with `env PATH=/usr/bin:/bin ./.venv/bin/pytest` → 61 pass, 4 skip, 0
fail. Also added the content assertions the existence-only tests lacked (duration ≈3s, audio
track present). `36b10a8`

**Lesson.** **Skip on the thing the test actually needs, not on a proxy for it.** A skip
condition that's a proxy will eventually diverge from reality and convert an intended skip into
a false failure — or worse, a false pass. And when a committed artifact is missing, a hard
failure is the *correct* outcome; the bug was making that outcome unreachable by accident.

---

### `astral-sh/setup-uv@v9` does not resolve

**Cycle:** Wave 0 / T01
**Cost:** ~5 min, 1 red CI run (failed at job setup in 7s)
**Category:** wrong-assumption

**Symptom.** `##[error]Unable to resolve action 'astral-sh/setup-uv@v9', unable to find version 'v9'`
on both runners.

**Root cause.** I read `releases/latest` → `v9.0.0` and assumed a floating `v9` major tag
existed, as is conventional for GitHub Actions. It doesn't: that repo publishes floating major
tags only through `v7`, while v8 and v9 exist solely as exact version tags. `actions/checkout@v7`
was fine because checkout *does* publish floating majors — so one bump in the same commit
worked and the other didn't, which is exactly the pattern that makes this easy to get wrong.

**Fix.** Pinned `astral-sh/setup-uv@v9.0.0` exactly, with a comment explaining why it's pinned
so nobody "tidies" it back to `@v9`. `7470a44`

**Lesson.** **`releases/latest` tells you the version; it does not tell you which refs are
resolvable.** Before bumping an action, check the actual tag list for the floating major
(`gh api repos/OWNER/REPO/tags --jq '.[].name' | grep -E '^v[0-9]+$'`), or just pin the exact
version. Pinning exactly is the better default for reproducibility anyway.

---

### Typer's `no_args_is_help` exits 2, not 0

**Cycle:** Wave 0 / T06
**Cost:** ~2 min, 1 red local run
**Category:** wrong-assumption

**Symptom.** `test_bare_invocation_shows_help` asserted `exit_code == 0`; got 2.

**Root cause.** Click implements `no_args_is_help` by raising a `UsageError`, which exits 2.
That's conventional and correct behavior — my expectation was wrong, not the framework.

**Fix.** Assert on the observable behavior that matters (`"Usage" in output`) rather than the
exit code. `8e70728`

**Lesson.** When testing a framework's behavior, assert the user-visible outcome rather than an
incidental mechanism. It's both more robust and a better description of the requirement.

---

### `git ls-files` is empty in a repo with no commits

**Cycle:** Wave 0 / T01
**Cost:** ~2 min
**Category:** environment

**Symptom.** `autoflake -i --remove-all-unused-imports $(git ls-files '*.py')` →
`autoflake: error: the following arguments are required: files`.

**Root cause.** `git init` had run but nothing was staged or committed yet, so `git ls-files`
listed nothing and the command substitution produced zero arguments.

**Fix.** Used `find src tests -name '*.py'` instead.

**Lesson.** Don't use `git ls-files` to enumerate files before the first commit. `find` is
unconditional; use it in bootstrap scripts and pre-commit tooling that might run on a virgin
repo.

---

### `cd /tmp && uv run` breaks project discovery

**Cycle:** Wave 0 / T06
**Cost:** ~2 min
**Category:** environment

**Symptom.** `cd /tmp && uv run story-book --help` → `error: Failed to spawn: story-book`.

**Root cause.** `uv run` resolves the project (and its console scripts) from the current
directory upward. Run from `/tmp`, there's no project, so the entry point doesn't exist.

**Fix.** Stay in the project directory and pass absolute paths to the CLI instead of changing
directory.

**Lesson.** Test a CLI on external paths by passing absolute arguments, not by `cd`-ing away
from the project. This generalizes: `cd` in a compound command is usually a mistake when the
tool being run is project-scoped.

---

### `BatchStage._run_batched` didn't return its report

**Cycle:** Wave 0 / T04
**Cost:** ~1 min
**Category:** wrong-test (by omission)

**Symptom.** None. Found by re-reading the method after writing it, before any test ran.

**Root cause.** Every other `_run_*` method ends in `return report`; this one fell off the end
and would have returned `None`, which `run()` would then have appended to the report list.

**Fix.** Added the missing return. `8e70728`

**Lesson.** The tests I'd written at that moment wouldn't have caught it, because I hadn't yet
written a `BatchStage` test that inspected the report. Worth noting as a coverage gap pattern:
when several methods share a shape, a test for one is not a test for the others. The
`TestBatchStage` class now asserts on report counts.

---

### NEAR-MISS: about to push a public repo with a work email in the commit author

**Cycle:** Wave 0 / release
**Cost:** ~0, caught before pushing
**Category:** near-miss

**Symptom.** Pre-push check showed `author: Victor Huang <victor.huang@carta.com>` on the only
commit of a personal side project about to be made public.

**Root cause.** Global git config is the work identity, and `git init` inherits it silently.
Nothing warns you.

**Fix.** Asked before pushing rather than proceeding. Set repo-local `user.name`/`user.email`
to a personal address and `git commit --amend --reset-author`. Verified afterwards that the
*remote* commit carried the right address, and that global config was untouched.

**Lesson.** **Check `git log -1 --format='%ae'` before the first push of any new repo**, and
set repo-local identity at `git init` time for anything personal. A single commit is a
one-command fix; after collaborators exist it's a history rewrite. More generally: for
irreversible outward-facing actions, spend the one round-trip to confirm the details the user
may not have thought about.

---

## Encoded as project rules

Lessons from this cycle that are now enforced rather than just written down:

- Stage cache identity is the `name` string — documented in `CLAUDE.md` and in
  `pipeline/base.py`'s module docstring.
- Committed fixtures are asserted, never skipped on a proxy condition — the `needs_ffmpeg`
  marker now guards only the checks that truly need the binary.
- GitHub Actions are pinned to exact versions, with a comment on the one that would otherwise
  look tidier as a floating major.
- Pre-push identity check is a `CLAUDE.md` rule.
