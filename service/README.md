# storybook-service

The HTTP service the iOS client talks to. Its whole job is to **run the existing `story-book`
CLI** — it reimplements no stage, owns no file under `src/story_book/`, and the laptop workflow
stays the reference implementation.

Design: [`../dev_plan/ios_backend_service.md`](../dev_plan/ios_backend_service.md).
Status and ownership: [`../dev_plan/ios_implementation_tracker.md`](../dev_plan/ios_implementation_tracker.md),
Wave S.

## What S01 settled

- **Python 3.12, FastAPI, uvicorn.** M1's work is `story-book build`; any other language shells out
  to the same CLI and loses the ability to read `stage_result` directly for job progress.
- **`service/` is a separate uv project** with a path dependency on the repository root. The root
  `pyproject.toml` is owned by the Python tracker and is untouched, and `uv run pytest` at the root
  still collects only `tests/` — the service suite runs from this directory.
- **One container image holds the API and the CLI.** That is the smallest deployment unit every
  candidate host accepts, so it does not presume which one is chosen.
- **Configuration is environment variables only**, prefixed `STORY_SERVICE_`. No secret has a
  default and none belongs in this tree.

## What S01 deliberately did not settle

Hosting, object storage, the queue substrate, and where the multi-trip index lives are open
questions 14–18 in the tracker. There is no object-store client and no queue in this directory on
purpose: S02–S07 would inherit whichever guess was made here.

## Run it locally

```bash
cd service
uv sync --extra dev
uv run pytest
uv run uvicorn storybook_service.app:app --reload --port 8000

curl -s localhost:8000/health   # liveness: the process answers, nothing more
curl -s localhost:8000/ready    # the dependency probe, and 503 if a required one is missing
```

**Launch through `uv run`, not `./.venv/bin/uvicorn`.** Invoking the binary by path does not put
`.venv/bin` on `PATH`, so the service starts fine, answers `/health` with `200`, and cannot find the
`story-book` executable it exists to run. `/ready` says so — `503`, with
`No such file or directory: 'story-book'` — which is the first thing this endpoint caught.

In a container (build from the repository root, because of the path dependency):

```bash
docker build -f service/Dockerfile -t storybook-service .
docker run --rm -p 8000:8000 storybook-service
docker run --rm storybook-service story-book --version
```

## `/ready` reports what this deployment can actually do

The pipeline **degrades rather than aborting** when an optional dependency is absent: a `clip`-less
image still returns a `trip.json`, with no CLIP clustering in it. So `/ready` separates the
dependencies a build cannot survive (`story-book` on PATH, `exiftool`, `ffmpeg`) from the ones that
only narrow the result (`reverse_geocoder`, `opencv`, `clip`, `faster_whisper`), reports each with
the consequence of its absence, and carries `measured_at` because the probe runs once at startup —
a cached reading must not read like a fresh one.

Every probe calls the predicate the corresponding stage calls in its own `available()` and quotes
that stage's own `description`. Re-deriving "is ffmpeg usable" here would be a second answer to a
question the pipeline already answers, and the two would drift.
