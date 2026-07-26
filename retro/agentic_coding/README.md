# Agentic coding retros

Errors, technical difficulties, and their resolutions from each dev/test cycle — so the next
cycle doesn't rediscover them.

## What belongs here

One file per dev/test cycle: `YYYY-MM-DD-<short-slug>.md`. Inside, one entry per problem that
cost real time or shipped a wrong result.

Write an entry when:

- A test failed and the **test** was wrong, not the code. These are the most valuable entries,
  because a wrong test is a wrong belief about the system.
- CI failed for a reason local runs couldn't catch.
- A dependency, binary, or action version behaved differently than assumed.
- An agent (or Claude) asserted something confidently that turned out to be false.
- A near-miss: something caught just before it became permanent or public.

Skip it when the fix was mechanical and carries no lesson — a typo, a formatting nit, a
rename. Volume is not the goal; a retro nobody reads is worse than no retro.

## What a good entry looks like

Be specific enough that someone can recognize the same failure next time, and honest about
the root cause. "Fixed a bug" teaches nothing. "Assumed X implied Y; they're independent"
teaches something.

```markdown
### <Short title stating the problem, not the fix>

**Cycle:** Wave 0 / T05
**Cost:** ~10 min, 2 red CI runs
**Category:** wrong-test | wrong-assumption | environment | tooling | near-miss | design-flaw

**Symptom.** What was observed, verbatim where useful (error text, exit code).

**Root cause.** Why it actually happened. Name the false belief if there was one.

**Fix.** What changed. Link the commit.

**Lesson.** The transferable rule. One or two sentences. If it should change how the project
is set up (a lint rule, a test, a CLAUDE.md line), say so and do it.
```

## Conventions

- Written after the fact, by whoever hit the problem — agent or human.
- Never rewrite history to look cleaner. An entry saying "I was confidently wrong about X" is
  the point of the exercise.
- If a lesson is durable and cheap to enforce, encode it: add the lint rule, add the test, add
  the line to `CLAUDE.md`. Then note in the entry that you did. A lesson only living in prose
  will be re-learned.

## Index

| Cycle | File | Entries |
| --- | --- | --- |
| Wave 0 — foundation | [2026-07-26-wave-0-foundation.md](./2026-07-26-wave-0-foundation.md) | 8 |
| T17 — profile command | [2026-07-26-t17-profile.md](./2026-07-26-t17-profile.md) | 2 |
| P01 — real trip data | [2026-07-26-p01-real-data.md](./2026-07-26-p01-real-data.md) | 3 + 1 note |
| Wave 1 integration | [2026-07-26-integration.md](./2026-07-26-integration.md) | 6 |
