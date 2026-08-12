### The shared local service was "confirmed alive" but could not run a job

**Cycle:** I30/I31/I33
**Cost:** ~20 min diagnosis, one throwaway trip on the shared instance, one disposable second
uvicorn process spun up to finish verification
**Category:** wrong-assumption, environment

**Symptom.** `POST /trips/{id}/build` against the shared service at `127.0.0.1:8000` (also bound
to the Mac's LAN IP) returned a bare `500 Internal Server Error` with no body. `GET /health` and
`GET /ready` both reported healthy immediately before and after.

**Root cause.** The task briefing said the service "was just restarted with S07's code live" —
true of the running *process*, confirmed independently via `/openapi.json` listing `POST
/trips/{trip_id}/reel`. But the on-disk `index.db` it opened was created by an earlier run, before
S07 added the `options` and `progress` columns to the `job` table, and nothing in
`index_sqlite.py` migrates an existing file — table creation is `CREATE TABLE IF NOT EXISTS`,
which does nothing once the table already exists in its old shape. So the worker's own background
loop (`requeue_stale`, which queries `j.options`) was failing on every tick, silently, into a log
file redirected to a path outside this session's view — `/health` and `/ready` cannot see this
because neither one queries the `job` table. "The code is current" and "the database it opens
matches that code" are two different facts, and only the first one is visible from outside.

**Fix.** Not fixed — `index_sqlite.py` belongs to S02/S03/S07, and recreating or migrating the
shared `index.db` is a decision about shared state other sessions might depend on, not something
to do unilaterally mid-task. Verification instead ran against a second, disposable `uvicorn`
process on a different port with an empty `STORY_SERVICE_DATA_ROOT` and the same
`STORY_SERVICE_OBJECT_STORE_BACKEND=local` — same code, fresh schema, zero risk to the shared
instance — which produced a real build and a real reel end to end. Logged in the tracker's Log
rather than silently worked around, since the next agent who reaches for the shared instance will
hit the exact same 500.

**Lesson.** *"Confirmed alive via `/health`" is a claim about the process, not about the data it
is about to touch.* A readiness probe that checks dependencies (`story-book`, `exiftool`, `ffmpeg`)
but never exercises the one write path that actually depends on schema (`enqueue_job` /
`requeue_stale`) will report healthy right up until the first real request. Worth a `/ready`
check that opens the job table and asks it something as cheap as `SELECT 1 FROM job LIMIT 1` — not
proposed as code here, since `/ready`'s owner is S01, but the gap is real and someone hitting this
service for a device test will lose the same 20 minutes without a log file to explain a bare 500.
