# 2026-08-09 — The package asked for the wrong thing, and got it

**Cycle:** T58, reported by the traveller after the Mammoth trip.

## The report

> ChatGPT initially gave us a separate `story.json` for each day, I had to ask it a second time
> to create a combined version.

My first instinct was to treat that as a model that had not followed the contract. It was not.

## What the package actually said

| Document | Instruction |
| --- | --- |
| `package/README.md` | "One directory per day. **For each day, open a fresh chat**" |
| `package/<date>/prompt.md` | "save your JSON as **`story.json`**", skeleton showing `"days": [{one date}]` |
| `report`, `reel`, `check-story` | read **one** `story.json` covering the whole trip |

Three chats, each correctly told to produce a file called `story.json` covering the day it was
given. Three files with the same name and nothing anywhere saying they were meant to become one.
The model did exactly as instructed; the instruction was wrong.

This is the same defect class as P07 — *a published request format ships with a published
response format* — with one difference that made it invisible. P07's mismatch was **key names**,
which `story.schema.json` pins. This one was **scope**, which the schema says nothing about: a
one-day story and a twenty-two-day story are both perfectly valid documents. `check-story` would
have passed all three files individually.

## The real cause

The package is organised one folder per day because that is how photographs accumulate and how a
large upload has to be broken up. Organising the *input* that way silently reorganised the
*output*. The day split is a size workaround; the unit of work is the trip.

The clearest proof is the trip title. It cannot be written from one day — day 1 does not know the
trip ends in Smith Valley — so per-day chats structurally cannot produce one.

And the sizes say the split was usually unnecessary anyway:

| Trip | Package | Days | Needed splitting? |
| --- | --- | --- | --- |
| Mammoth | 73 MB | 3 | **No** — one upload, and the split cost a second ask for nothing |
| Europe 2026 | 721 MB raw | 21 | Yes — 2 parts |

## The fix

One `prompt.md` at the package root asking for exactly one `story.json`, with every date
pre-listed in the `days` skeleton so "all N must be present" is checkable by eye. Per-day
`prompt.md` files removed — with a root prompt they contradict it. `brief.md` and contact sheets
stay per day, because that is genuinely per-day content.

`--zip` now writes one archive when the package fits under `--max-part-mb` (default 200) and
`package.partNofM.zip` when it does not, splitting **on day boundaries only**. A brief maps cells
to asset ids; separated from its sheets it is unusable, so a day is the smallest divisible unit.
A day larger than the limit gets its own oversized part and is *reported*.

## What I'd have missed without the traveller

Every test asserted the prompt's **content** — that it names the required keys, asks for a
storyboard, refuses to invent feelings. Not one asked how many prompts there were, or what scope
the answer should cover. A per-day prompt asking for a per-day file is internally consistent;
only a consumer notices that the tool reads one file.

The standing rule already covers it — *"read generated prose in the role of its consumer"* — but
I had read `prompt.md` as the model, checking whether I could answer it. I had not read
`README.md` as the traveller, asking what workflow it puts me through.

## Encoded

- `TestOneStoryPerTrip` — the prompt is at the root, no day carries one, it asks for exactly one
  file, it names every date, and the README no longer says "fresh chat".
- `TestArchiveParts` + `tests/unit/test_package_parts.py` — every file lands in exactly one part,
  no day is divided, shared files travel with part 1, an oversized day is reported.
- New line in `CLAUDE.md`: **organising the input silently reorganises the output.**

## Verified on real data

Mammoth 73 MB → one zip. Europe → 2 parts (187 + 65 MB); 800 files on disk, 800 across the parts,
zero overlap, zero days split.
