"""Truth-set format and evaluation harness (T18).

Scores the pipeline against a hand-labelled truth set for the three modules Phase 1's success
criteria name: event detection (Module 6), near-duplicate clustering (Module 7), and selection
(Module 10). See `docs/truth_set.md` for the file format and labelling guidance.

T23 (dedup), T24 (events), and T30 (selection) do not exist yet at the time this module is
written, so every scorer below must work against an empty or partially-populated database: it
reports `computed=False` with an explanatory note instead of crashing when the relevant tables
are empty. `tests/backend/test_eval.py` populates `event`/`cluster`/`selection` directly to
exercise the scoring logic ahead of those stages landing.

Rendering is kept separate from computation (`render_report` vs `evaluate`) so the numbers stay
testable without string-matching.
"""

from __future__ import annotations

import sqlite3
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from story_book.db import connection as db

# Targets named in the plan doc's "Success criteria for Phase 1".
EVENT_TARGET_PRECISION = 0.80
EVENT_TARGET_RECALL = 0.80
KEEPER_TARGET_AGREEMENT = 0.70


class TruthSetError(Exception):
    """Raised for a malformed truth-set file."""


# --------------------------------------------------------------------------------------
# Truth-set format
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class DuplicateGroupSpec:
    """One hand-labelled near-duplicate group. `keep` is optional -- a human may not have
    decided on a preferred pick yet, and the format must tolerate that partial state."""

    members: list[str]
    keep: str | None = None


@dataclass(slots=True)
class TruthSet:
    """A hand-labelled truth set. Every section is independently optional: a file may label
    events but not duplicates, or vice versa. See `docs/truth_set.md`."""

    trip_name: str | None = None
    notes: str | None = None
    events: list[list[str]] = field(default_factory=list)
    duplicate_groups: list[DuplicateGroupSpec] = field(default_factory=list)
    distinct_pairs: list[tuple[str, str]] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.events and not self.duplicate_groups and not self.distinct_pairs


def load_truth_set(path: Path) -> TruthSet:
    """Parse a hand-written truth-set TOML file. See `docs/truth_set.md` for the format."""
    if not path.exists():
        raise TruthSetError(f"truth set not found: {path}")
    with path.open("rb") as handle:
        try:
            raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise TruthSetError(f"malformed TOML in {path}: {exc}") from exc
    return _truth_set_from_dict(raw)


def _truth_set_from_dict(raw: dict[str, Any]) -> TruthSet:
    trip = raw.get("trip", {}) or {}
    if not isinstance(trip, dict):
        raise TruthSetError("[trip] must be a table")

    events: list[list[str]] = []
    for entry in raw.get("event", []) or []:
        items = entry.get("items")
        if not isinstance(items, list) or not items:
            raise TruthSetError(f"[[event]] entry needs a non-empty 'items' list: {entry!r}")
        events.append([str(item) for item in items])

    duplicate_groups: list[DuplicateGroupSpec] = []
    for entry in raw.get("duplicate_group", []) or []:
        members = entry.get("members")
        if not isinstance(members, list) or len(members) < 2:
            raise TruthSetError(f"[[duplicate_group]] needs at least 2 'members': {entry!r}")
        members = [str(m) for m in members]
        keep = entry.get("keep")
        if keep is not None:
            keep = str(keep)
            if keep not in members:
                raise TruthSetError(f"duplicate_group 'keep' ({keep!r}) is not in 'members'")
        duplicate_groups.append(DuplicateGroupSpec(members=members, keep=keep))

    distinct_pairs: list[tuple[str, str]] = []
    for entry in raw.get("distinct_pair", []) or []:
        pair = entry.get("pair")
        if not isinstance(pair, list) or len(pair) != 2:
            raise TruthSetError(f"[[distinct_pair]] needs a 'pair' of exactly 2 items: {entry!r}")
        distinct_pairs.append((str(pair[0]), str(pair[1])))

    hashes_raw = raw.get("hashes", {}) or {}
    if not isinstance(hashes_raw, dict):
        raise TruthSetError("[hashes] must be a table")
    hashes = {str(k): str(v) for k, v in hashes_raw.items()}

    return TruthSet(
        trip_name=trip.get("name"),
        notes=trip.get("notes"),
        events=events,
        duplicate_groups=duplicate_groups,
        distinct_pairs=distinct_pairs,
        hashes=hashes,
    )


# --------------------------------------------------------------------------------------
# Resolving filenames to media hashes
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Resolution:
    """The result of matching a truth set's filenames against the DB's media."""

    hash_by_filename: dict[str, str]
    unmatched: list[str]
    ambiguous: list[str]


def _filenames_in(truth: TruthSet) -> set[str]:
    names: set[str] = set()
    for group in truth.events:
        names.update(group)
    for dup in truth.duplicate_groups:
        names.update(dup.members)
    for a, b in truth.distinct_pairs:
        names.update((a, b))
    names.update(truth.hashes)
    return names


def resolve_truth_set(conn: sqlite3.Connection, truth: TruthSet) -> Resolution:
    """Match truth-set filenames against media in the DB.

    Filenames are the format's primary key because a human labelling from a file browser sees
    filenames, not content hashes -- see `docs/truth_set.md` for the tradeoff. That's fragile if
    two different source folders both contain an `IMG_0001.jpg`; the optional `[hashes]` table
    disambiguates by content hash for anyone who wants that extra robustness. Most truth sets
    won't need it.
    """
    index: dict[str, list[str]] = defaultdict(list)
    for media in db.iter_media(conn):
        index[Path(media.path).name].append(media.hash)

    hash_by_filename: dict[str, str] = {}
    unmatched: list[str] = []
    ambiguous: list[str] = []
    for filename in sorted(_filenames_in(truth)):
        candidates = index.get(filename, [])
        if not candidates:
            unmatched.append(filename)
            continue
        if len(candidates) == 1:
            hash_by_filename[filename] = candidates[0]
            continue
        hint = truth.hashes.get(filename)
        if hint in candidates:
            hash_by_filename[filename] = hint
        else:
            ambiguous.append(filename)
            hash_by_filename[filename] = sorted(candidates)[0]

    return Resolution(hash_by_filename=hash_by_filename, unmatched=unmatched, ambiguous=ambiguous)


# --------------------------------------------------------------------------------------
# Event boundary scoring -- pure maths, no DB
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class BoundaryScore:
    computed: bool
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    true_boundaries: int = 0
    predicted_boundaries: int = 0
    correct_boundaries: int = 0
    pairs_scored: int = 0
    target_met: bool | None = None
    note: str = ""


def score_boundaries(true_groups: list[Any], pred_groups: list[Any]) -> BoundaryScore:
    """Precision/recall over adjacent-pair event boundaries in a fixed chronological order.

    `true_groups[i]` / `pred_groups[i]` are the true/predicted event identifiers of the i-th
    item in time order. A boundary exists between item i and i+1 when consecutive identifiers
    differ. This is what the plan doc means by "a merged pair counts as one miss": two truly
    separate events the pipeline merges into one lose exactly the one boundary between them,
    not one miss per pair of items on either side of it.
    """
    if len(true_groups) != len(pred_groups):
        raise ValueError("true_groups and pred_groups must be the same length")
    if len(true_groups) < 2:
        return BoundaryScore(computed=False, note="fewer than 2 items to compare")

    true_boundary = [a != b for a, b in zip(true_groups, true_groups[1:], strict=False)]
    pred_boundary = [a != b for a, b in zip(pred_groups, pred_groups[1:], strict=False)]
    correct = sum(1 for t, p in zip(true_boundary, pred_boundary, strict=True) if t and p)
    n_true = sum(true_boundary)
    n_pred = sum(pred_boundary)

    # Standard vacuous-denominator convention, independent of the other side: a metric with
    # nothing in its own denominator (no predictions made / nothing to find) is trivially
    # perfect, regardless of what the other metric says.
    precision = correct / n_pred if n_pred else 1.0
    recall = correct / n_true if n_true else 1.0
    return BoundaryScore(
        computed=True,
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        true_boundaries=n_true,
        predicted_boundaries=n_pred,
        correct_boundaries=correct,
        pairs_scored=len(true_boundary),
        target_met=precision >= EVENT_TARGET_PRECISION and recall >= EVENT_TARGET_RECALL,
    )


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# --------------------------------------------------------------------------------------
# Duplicate-group scoring -- pure maths, no DB
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class PairwiseScore:
    computed: bool
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    true_positive_pairs: int = 0
    predicted_positive_pairs: int = 0
    true_positive_total: int = 0
    note: str = ""


def score_pairwise_clusters(
    true_groups: dict[str, Any], pred_groups: dict[str, Any]
) -> PairwiseScore:
    """Pairwise precision/recall over "same cluster" item pairs.

    Chosen over Adjusted Rand Index. The plan doc calls out an asymmetry ARI would hide: "no
    false merges of visually distinct subjects matters more than perfect recall". Pairwise
    precision is exactly "of the pairs the pipeline put in one cluster, how many really are
    duplicates" -- a direct false-merge rate -- and pairwise recall is "of the true duplicate
    pairs, how many did it find". ARI folds both into one chance-corrected scalar, which can't
    distinguish "occasionally over-merges distinct subjects" from "occasionally splits real
    duplicates" -- exactly the distinction Module 7's acceptance criterion cares about.
    """
    keys = sorted(true_groups)
    if keys != sorted(pred_groups):
        raise ValueError("true_groups and pred_groups must cover the same keys")
    if len(keys) < 2:
        return PairwiseScore(computed=False, note="fewer than 2 items to compare")

    tp = fp = fn = 0
    for a, b in combinations(keys, 2):
        same_true = true_groups[a] == true_groups[b]
        same_pred = pred_groups[a] == pred_groups[b]
        if same_true and same_pred:
            tp += 1
        elif same_pred and not same_true:
            fp += 1
        elif same_true and not same_pred:
            fn += 1

    predicted_positive = tp + fp
    true_positive_total = tp + fn
    # Standard vacuous-denominator convention, independent of the other side: a metric with
    # nothing in its own denominator (no predictions made / nothing to find) is trivially
    # perfect, regardless of what the other metric says.
    precision = tp / predicted_positive if predicted_positive else 1.0
    recall = tp / true_positive_total if true_positive_total else 1.0
    return PairwiseScore(
        computed=True,
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        true_positive_pairs=tp,
        predicted_positive_pairs=predicted_positive,
        true_positive_total=true_positive_total,
    )


# --------------------------------------------------------------------------------------
# Keeper agreement -- pure maths, no DB
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class KeeperRecord:
    group_index: int
    true_keep: str
    predicted_keep: str | None
    resolvable: bool


@dataclass(slots=True)
class KeeperScore:
    computed: bool
    agreement: float | None = None
    matched: int = 0
    total: int = 0
    unresolved: int = 0
    target_met: bool | None = None
    note: str = ""


def score_keeper_agreement(records: list[KeeperRecord]) -> KeeperScore:
    """Fraction of duplicate groups where the pipeline's chosen keeper matches the human pick."""
    resolvable = [r for r in records if r.resolvable]
    if not resolvable:
        return KeeperScore(
            computed=False,
            total=len(records),
            note="no resolvable duplicate groups with a preferred pick",
        )
    matched = sum(1 for r in resolvable if r.predicted_keep == r.true_keep)
    agreement = matched / len(resolvable)
    return KeeperScore(
        computed=True,
        agreement=agreement,
        matched=matched,
        total=len(resolvable),
        unresolved=len(records) - len(resolvable),
        target_met=agreement >= KEEPER_TARGET_AGREEMENT,
    )


# --------------------------------------------------------------------------------------
# DB-facing wrappers -- read event/cluster/selection tables, never media/stage_result raw
# --------------------------------------------------------------------------------------


def _media_event_map(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT media_hash, event_id FROM media_event").fetchall()
    return {row["media_hash"]: row["event_id"] for row in rows}


def _media_cluster_map(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT media_hash, cluster_id FROM media_cluster").fetchall()
    return {row["media_hash"]: row["cluster_id"] for row in rows}


def _cluster_keepers(conn: sqlite3.Connection) -> dict[int, str | None]:
    rows = conn.execute("SELECT id, keeper_hash FROM cluster").fetchall()
    return {row["id"]: row["keeper_hash"] for row in rows}


def _has_any_events(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM event LIMIT 1").fetchone() is not None


def _has_any_clusters(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM cluster LIMIT 1").fetchone() is not None


def evaluate_events(
    conn: sqlite3.Connection, truth: TruthSet, resolution: Resolution
) -> BoundaryScore:
    if not truth.events:
        return BoundaryScore(computed=False, note="truth set has no [[event]] labels")

    hash_to_group: dict[str, int] = {}
    for index, group in enumerate(truth.events):
        for filename in group:
            media_hash = resolution.hash_by_filename.get(filename)
            if media_hash is not None:
                hash_to_group[media_hash] = index

    if len(hash_to_group) < 2:
        return BoundaryScore(
            computed=False, note="fewer than 2 labelled event items resolved to media in the DB"
        )
    if not _has_any_events(conn):
        return BoundaryScore(computed=False, note="pipeline has not produced any events yet")

    media_by_hash = {m.hash: m for m in db.iter_media(conn) if m.hash in hash_to_group}
    ordered = sorted(
        hash_to_group,
        key=lambda h: (media_by_hash[h].taken_utc is None, media_by_hash[h].taken_utc or "", h),
    )
    pred_event = _media_event_map(conn)
    true_seq = [hash_to_group[h] for h in ordered]
    pred_seq = [pred_event.get(h, f"__unassigned__{h}") for h in ordered]
    return score_boundaries(true_seq, pred_seq)


def evaluate_duplicates(
    conn: sqlite3.Connection, truth: TruthSet, resolution: Resolution
) -> PairwiseScore:
    if not truth.duplicate_groups and not truth.distinct_pairs:
        return PairwiseScore(
            computed=False, note="truth set has no duplicate_group or distinct_pairs labels"
        )

    true_groups: dict[str, Any] = {}
    next_singleton = 0

    def new_singleton() -> int:
        nonlocal next_singleton
        next_singleton -= 1
        return next_singleton

    for index, group in enumerate(truth.duplicate_groups):
        for filename in group.members:
            media_hash = resolution.hash_by_filename.get(filename)
            if media_hash is not None:
                true_groups[media_hash] = index

    for a, b in truth.distinct_pairs:
        for filename in (a, b):
            media_hash = resolution.hash_by_filename.get(filename)
            if media_hash is not None and media_hash not in true_groups:
                true_groups[media_hash] = new_singleton()

    if len(true_groups) < 2:
        return PairwiseScore(
            computed=False, note="fewer than 2 labelled duplicate items resolved to media in the DB"
        )
    if not _has_any_clusters(conn):
        return PairwiseScore(computed=False, note="pipeline has not produced any clusters yet")

    pred_cluster = _media_cluster_map(conn)
    pred_groups = {h: pred_cluster.get(h, f"__unclustered__{h}") for h in true_groups}
    return score_pairwise_clusters(true_groups, pred_groups)


def evaluate_keeper_agreement(
    conn: sqlite3.Connection, truth: TruthSet, resolution: Resolution
) -> KeeperScore:
    groups_with_keep = [g for g in truth.duplicate_groups if g.keep]
    if not groups_with_keep:
        return KeeperScore(computed=False, note="truth set has no duplicate_group with 'keep' set")
    if not _has_any_clusters(conn):
        return KeeperScore(
            computed=False,
            total=len(groups_with_keep),
            note="pipeline has not produced any clusters yet",
        )

    pred_cluster = _media_cluster_map(conn)
    keepers = _cluster_keepers(conn)

    records: list[KeeperRecord] = []
    for index, group in enumerate(groups_with_keep):
        true_keep_hash = resolution.hash_by_filename.get(group.keep)
        member_hashes = [
            resolution.hash_by_filename[m]
            for m in group.members
            if m in resolution.hash_by_filename
        ]
        if true_keep_hash is None or not member_hashes:
            records.append(KeeperRecord(index, group.keep, None, resolvable=False))
            continue
        cluster_votes = Counter(pred_cluster[h] for h in member_hashes if h in pred_cluster)
        if not cluster_votes:
            records.append(KeeperRecord(index, true_keep_hash, None, resolvable=False))
            continue
        majority_cluster, _ = cluster_votes.most_common(1)[0]
        predicted_keep = keepers.get(majority_cluster)
        records.append(KeeperRecord(index, true_keep_hash, predicted_keep, resolvable=True))

    return score_keeper_agreement(records)


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class EvalReport:
    """Structured scoring result. Rendering (`render_report`) is a separate step so the numbers
    stay testable without string-matching."""

    truth_set_path: Path | None
    events: BoundaryScore
    duplicates: PairwiseScore
    keeper_agreement: KeeperScore
    unmatched_filenames: list[str] = field(default_factory=list)
    ambiguous_filenames: list[str] = field(default_factory=list)


def evaluate(
    conn: sqlite3.Connection, truth: TruthSet, *, truth_set_path: Path | None = None
) -> EvalReport:
    """Score a truth set against whatever the pipeline has computed so far.

    Safe to call against an empty or partially-populated DB: each metric reports
    `computed=False` with a `note` rather than raising, since the stages that produce
    `event`/`cluster`/`selection` rows (T23/T24/T30) may not have run yet.
    """
    resolution = resolve_truth_set(conn, truth)
    return EvalReport(
        truth_set_path=truth_set_path,
        events=evaluate_events(conn, truth, resolution),
        duplicates=evaluate_duplicates(conn, truth, resolution),
        keeper_agreement=evaluate_keeper_agreement(conn, truth, resolution),
        unmatched_filenames=resolution.unmatched,
        ambiguous_filenames=resolution.ambiguous,
    )


def evaluate_truth_set_file(conn: sqlite3.Connection, path: Path) -> EvalReport:
    """Load a truth-set TOML file and score it against the DB.

    The one function a CLI `eval` command needs: `evaluate_truth_set_file(conn, path)` then
    `render_report(report)` to print it. Not wired into `cli.py` here -- that's the
    integrator's job.
    """
    truth = load_truth_set(path)
    return evaluate(conn, truth, truth_set_path=path)


def render_report(report: EvalReport) -> str:
    """Human-readable text rendering. Kept separate from `evaluate` on purpose."""
    lines: list[str] = []
    if report.truth_set_path:
        lines.append(f"Truth set: {report.truth_set_path}")
    lines.append(
        _render_boundary("Event boundaries (target >=80% precision/recall)", report.events)
    )
    lines.append(
        _render_pairwise("Duplicate groups (pairwise precision/recall)", report.duplicates)
    )
    lines.append(_render_keeper("Keeper agreement (target >=70%)", report.keeper_agreement))
    if report.unmatched_filenames:
        lines.append(
            "Unmatched filenames (not found in DB): " + ", ".join(report.unmatched_filenames)
        )
    if report.ambiguous_filenames:
        lines.append(
            "Ambiguous filenames (matched multiple hashes, best guess used): "
            + ", ".join(report.ambiguous_filenames)
        )
    return "\n".join(lines)


def _render_boundary(title: str, score: BoundaryScore) -> str:
    if not score.computed:
        return f"{title}: not yet computed ({score.note})"
    status = "MET" if score.target_met else "NOT MET"
    return (
        f"{title}: precision={score.precision:.0%} recall={score.recall:.0%} "
        f"f1={score.f1:.0%} [{status}] ({score.correct_boundaries}/{score.predicted_boundaries} "
        f"predicted boundaries correct, {score.correct_boundaries}/{score.true_boundaries} true "
        f"boundaries found, over {score.pairs_scored} adjacent pairs)"
    )


def _render_pairwise(title: str, score: PairwiseScore) -> str:
    if not score.computed:
        return f"{title}: not yet computed ({score.note})"
    return (
        f"{title}: precision={score.precision:.0%} recall={score.recall:.0%} f1={score.f1:.0%} "
        f"({score.true_positive_pairs}/{score.predicted_positive_pairs} predicted pairs correct, "
        f"{score.true_positive_pairs}/{score.true_positive_total} true pairs found)"
    )


def _render_keeper(title: str, score: KeeperScore) -> str:
    if not score.computed:
        return f"{title}: not yet computed ({score.note})"
    status = "MET" if score.target_met else "NOT MET"
    return (
        f"{title}: agreement={score.agreement:.0%} [{status}] "
        f"({score.matched}/{score.total} groups, {score.unresolved} unresolved)"
    )
