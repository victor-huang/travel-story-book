# 2026-08-09 — `init` scaffolded a config that failed the first build

**Cycle:** T56, `story-book init`.

## What happened

`init` writes two files into a new trip directory. The config half was careful: it rewrites the
shipped example so the explanatory comments survive, refuses to write a key the example does not
contain, and labels each value with where it came from.

The corrections half was one line — `overrides_path.write_text(EXAMPLE_OVERRIDES.read_text())`.

Twelve tests passed. Then I ran it on the fixtures and built:

```
events    failed  OverrideError: pin: no media in this library is named 'IMG_1880'
timeline  failed  OverrideError: pin: no media in this library is named 'IMG_1880'
```

`overrides.example.toml` is not a template. It is a **worked example**, with live `pin`,
`reject`, `keeper`, `split_event` and `label_event` entries naming real photographs from the
Europe 2026 trip. Copied into a new trip it does not scaffold anything — it guarantees an
`OverrideError` on the first run, in the *first command the tool tells you to run next*.

## Why the tests did not catch it

Every test I wrote asked whether the file was **present**. `test_creates_both_files` asserts
`overrides_path.is_file()`. `test_keeps_corrections_a_person_already_wrote` asserts we do not
clobber. Not one asked what was *in* it, because I knew what was in it — I had just written the
copy.

This is the same defect class as P06's nine JPEGs under `.mov` names: a schema validated, every
reference resolved, 87 tests passed, all of them checking presence and none checking identity.
The rule already in `CLAUDE.md` is "for every declared media type in an export, verify one
file's actual bytes." The generalisation it was missing: **when you copy a file into a new
context, load it in that new context.** `Overrides.load(scaffolded).is_empty` is one line and
would have failed immediately.

## The fix, and why it is not a second file

The obvious repair is a hand-written `overrides.starter.toml` with everything commented out.
That is two copies of one file, which this project has already decided is one copy eventually
wrong — hence the existing drift test on `trip_context.example.toml`.

Instead `starter_overrides()` derives the starter from the example by commenting out every
non-comment line except `override_version`. A section added to the example cannot go missing
from the starter, because there is nothing to keep in sync.

## Encoded

- `TestStarterOverrides::test_it_selects_nothing` — `Overrides.load()` on the scaffolded file is
  empty. **Shown to fail against the old behaviour**: verbatim example `is_empty: False`,
  starter `is_empty: True`.
- `test_no_example_filename_survives_uncommented` — the only live line is `override_version = 1`.
- New line for `CLAUDE.md`: *a file that is safe to read is not automatically safe to copy.*

## Two smaller things the same run surfaced

- **Rich ate `[home]` as a markup tag** in the "no [home] block" warning, so the sentence
  rendered without the word it was about. Third time this project has hit Rich markup
  swallowing a bracketed literal (`[zh]` in the reel, `[home]` twice here). Escape it: `\[home]`.
- **The printed next-steps commands wrapped** at the terminal width and could not be
  copy-pasted, which is the entire reason for printing them. `soft_wrap=True`. A convenience
  feature that produces something unusable is not a smaller version of the feature.

## Cost

About 20 minutes, all of it after the tests were green. The bug was invisible to the test suite
and obvious the first time the command was run for real — which is the standing lesson here,
again: **look at the output**.
