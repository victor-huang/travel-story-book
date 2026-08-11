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

## What S02 added, and what it still refuses to decide

Ingest: create a trip, negotiate by content hash, hand back a presigned `PUT` per asset, then
materialise the folder and scaffold a config over it. **The API server never carries the media** —
there is no route here that accepts bytes, and a test asserts that by reading the OpenAPI document
rather than by intention.

Settled, because question 15 settled it: **AWS S3**, presigned `PUT`, bucket and region as
configuration with no defaults, credentials by IAM instance role (there is no field for a key).

Not settled, and both need a human:

- **Which engine holds the index.** `index.py` is a five-method interface; `index_sqlite.py` is the
  one implementation, chosen because local development needs nothing installed. `index.for_dsn`
  refuses a `postgresql://` DSN *by name* rather than falling back — a silent fallback to SQLite on
  a production host would look like a working deployment. Open question 19.
- **Whether assets are keyed per user or globally by content** (question 4). Both layouts are in
  `naming.asset_key` and the default is `user`, for a reason beyond storage cost: the service never
  reads the bytes, so it **cannot verify that the object under a hash hashes to it**. Under a global
  layout one account could PUT chosen bytes at another's key. Per-user keeps cross-*trip* dedup —
  which is the case the design doc actually promises — and gives up only cross-*user* dedup.

## What S03 added: the queue, the worker, and honest progress

`POST /trips/{id}/build` queues one job; a worker claims it, fetches the uploaded media, scaffolds
the config and runs **`story-book build`** as a subprocess. `GET /jobs/{id}` reports where it is.

- **The queue is a table in the index**, claimed in one transaction. No broker: the index is already
  durable, the worker needs the same filesystem the build writes to, and on one EC2 instance
  (question 14) a broker buys distribution this deployment cannot use. **This is a proposal awaiting
  ratification (question 16), and it expires when a second instance exists.**
- **"One worker per trip at a time" is a partial unique index**, not a check in a route: a trip may
  have at most one job that is `queued` or `running`. A second build request returns that job with
  `created: false`.
- **The worker runs in a thread of the API process by default** (`STORY_SERVICE_WORKER_INLINE=0` to
  turn it off) and `python -m storybook_service.worker` runs it beside the API instead. The claim
  transaction makes both correct. Inline is the default because the other default fails silently: a
  queued job nothing ever picks up, on a service whose `/health` says `ok`.
- **A retry is a resume.** Nothing deletes or rewrites `--out`. Exit `130` (the CLI's interrupted
  code) requeues rather than fails, and a worker that stops heartbeating has its job requeued after
  `JOB_HEARTBEAT_TIMEOUT_S` — the pipeline commits per item, so the next attempt carries on.
- **A failed job never reads like a finished one.** Exit non-zero, or exit 0 with no `trip.json`,
  is `failed` with the build's own last words in `error`.

### Progress is read, never invented

`GET /jobs/{id}` publishes `{state, stage, done, total}` — the contract I22 asks for — plus a
per-stage breakdown. Every count is a row count in the trip's `story.db`, taken with the pipeline's
own accessors (`completed_hashes`, `stage_failures`, `count_media`) on a **read-only** connection.

**There is no percentage, no ETA and no smoothing**, and that is deliberate: eighteen stages cost
wildly different amounts per item, so one number over all of them would be a fabricated measurement.
`stage_index` / `stages_total` is the honest way to show overall position. `total` is `null` where
nothing has been measured — never a substituted `0`, which reads as "nothing to do".

Two denominators are subtler than they look, and both have a test:

- `EmbeddingStage.select()` filters out what it has already embedded, so `len(select())` *shrinks*
  as it progresses. The published total is `select() ∪ already-completed`, which cannot walk down
  towards `done`.
- Before `scan` commits, the media table is empty and every per-item stage selects nothing. The
  first version of this reported five of eighteen stages **complete** one second into a build. An
  empty library is not evidence that a stage has nothing to do.

`degraded` and `unavailable_stages` answer "was this build as good as this pipeline gets?", measured
at the job's start by calling each stage's own `available()` and quoting its reason. This image has
no `clip` (question 18 is still open), so a real job reports `embeddings` and `content_class`
unavailable with *"CLIP unavailable: missing torch, open_clip"* — a narrower result, said out loud.

## Run it locally

```bash
cd service
uv sync --extra dev
uv run pytest
uv run uvicorn storybook_service.app:app --reload --port 8000

curl -s localhost:8000/health   # liveness: the process answers, nothing more
curl -s localhost:8000/ready    # the dependency probe, and 503 if a required one is missing
```

For the ingest routes, point it at a local S3. MinIO or `moto server` both work:

```bash
export STORY_SERVICE_S3_BUCKET=story-media
export STORY_SERVICE_S3_REGION=eu-central-1
export STORY_SERVICE_S3_ENDPOINT_URL=http://127.0.0.1:5000   # unset in production
export AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local
```

Without a bucket the service still starts, `/health`, `/ready` and the trip list still answer, and
the ingest routes return `503` naming the variable to set. The bucket does not exist yet.

## There is no authentication yet

**Every caller is believed.** `principal.py` reads an `X-Story-Identity` header — an email address or
a phone number, per question 17 — and creates or finds that user. S06 replaces the body of one
function with verified Google (then Apple) sign-in; it does not have to touch a query, because every
index read is already scoped by `owner_id` **in SQL** rather than by a caller remembering to filter.
The service logs a warning saying all of this at every start, and a test asserts the warning.

## The wire contract

```
POST /trips                            {name}                   -> 201 {trip_id, name, created_at}
GET  /trips                                                     -> 200 {trips: [...]}
GET  /trips/{trip_id}                                           -> 200 {trip, assets: [...]}
POST /trips/{trip_id}/assets:negotiate {assets:[{hash,filename,size}]}
                                                                -> 200 {needed: [...], have: [...]}
PUT  <put_url>                         the bytes, to S3         -> not this service
POST /trips/{trip_id}/source:prepare                            -> 200 {fetched, missing, config}
POST /trips/{trip_id}/build            {}                       -> 202 {job_id, state, created}
                                                                -> 200 when one is already active
GET  /trips/{trip_id}/jobs                                      -> 200 {jobs: [...]} newest first
GET  /jobs/{job_id}                                             -> 200 {state, stage, done, total,
                                                                        stage_index, stages_total,
                                                                        stages: [...], degraded,
                                                                        unavailable_stages, error}
```

`POST .../build` takes **no body fields** — not the `{config, overrides}` the design doc shows. The
config a build uses is the one `story-book init` *measured* from the uploaded media; accepting one
from the client would replace measurements with a cached guess, permanently, because `init` will not
overwrite its own file. Config ownership is open question 8.

`state` is `queued | running | succeeded | failed`. `stage` is `null` while queued, `source:prepare`
while the media is being fetched, and otherwise the first pipeline stage with outstanding work.

`hash` is 128 lowercase hex characters — `hashlib.blake2b()`'s default 64-byte digest. An `asset_id`
prefix is **rejected**, not silently unmatched: accepting one would report every asset as needed
forever with nothing anywhere saying why.

`needed` entries carry `put_url`, the `headers` the client must reproduce (the `Content-Length` is
part of the signature), `expires_at`, and `stored_filename` — which differs from `filename` only when
two assets in one trip claim the same name, and then `filename_adjusted` is true.

`have` means **the store holds an object of the declared length at that key**. It does not mean the
bytes hash to it, and the response says so in `upload.presence_not_verified`.

`source:prepare` is an addition to the endpoint list in the design doc. It is idempotent, and S03 may
fold it into `build`; see the tracker's S02 entry for why it is a route rather than a hidden step.

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
