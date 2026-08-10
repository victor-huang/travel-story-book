"""Subtitle tracks for the reel, in any language the story carries a translation for.

The reel's own text is English (or whatever `story.json` is written in): title cards for the trip
and each day, and a caption under each photograph. This turns that text into timed cues so a
viewer can read it in another language while the picture stays as it is.

Design notes:

* **Soft tracks, not burned-in pixels.** A `.vtt` beside the video plus an optional `mov_text`
  track inside it. The player draws the text, so nothing here needs a CJK font -- and a viewer can
  turn it off or pick a different language, which burned-in text can never offer.
* **Never label translated text with a language it is not in.** If the story carries no
  translation for a language, no track is written for it. A Chinese subtitle track full of English
  is the artifact overstating its contents, which is the failure this project keeps guarding
  against. Partial translation is allowed, reported, and falls back per cue.
* **Burn-in composites Pillow-drawn PNGs, not ffmpeg's `subtitles` filter**, which needs a build
  with libass -- a stock Homebrew ffmpeg has no such filter at all. Same reasoning as the title
  cards, and it means the font is chosen by what has to be drawn.
* **Cues are clamped so they never overlap.** Segments overlap by `crossfade_seconds`, so the
  naive span (offset -> offset + duration) would put two cues on screen at once.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from story_book.export.fonts import font_for, renderable

logger = logging.getLogger(__name__)

SOURCE_LANGUAGE_DEFAULT = "en"

# mov_text metadata wants ISO 639-2. Only languages we can name confidently are mapped; anything
# else is passed through, since a wrong three-letter code is worse than an unrecognised one.
ISO_639_2 = {
    "en": "eng",
    "zh": "zho",
    "zh-hans": "zho",
    "zh-hant": "zho",
    "ja": "jpn",
    "ko": "kor",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "pt": "por",
    "nl": "nld",
    "ru": "rus",
    "ar": "ara",
    "hi": "hin",
    "vi": "vie",
    "th": "tha",
}


@dataclass(frozen=True, slots=True)
class Cue:
    start: float
    end: float
    text: str
    translated: bool
    """False when this cue fell back to the story's own language."""


@dataclass(slots=True)
class SubtitleTrack:
    language: str
    cues: list[Cue] = field(default_factory=list)

    @property
    def translated_count(self) -> int:
        return sum(1 for c in self.cues if c.translated)

    @property
    def fully_translated(self) -> bool:
        return bool(self.cues) and self.translated_count == len(self.cues)


def source_language(story: dict | None) -> str:
    return ((story or {}).get("language") or SOURCE_LANGUAGE_DEFAULT).lower()


def _translation(record: dict, language: str) -> str | None:
    """A translation for `language`, accepting either a bare string or a `{field: text}` object."""
    table = record.get("translations")
    if not isinstance(table, dict):
        return None
    value = table.get(language) or table.get(language.lower())
    if isinstance(value, str):
        return value.strip() or None
    return None


def _translated_field(record: dict, language: str, field_name: str) -> str | None:
    table = record.get("translations")
    if not isinstance(table, dict):
        return None
    value = table.get(language) or table.get(language.lower())
    if isinstance(value, dict):
        text = value.get(field_name)
        return text.strip() if isinstance(text, str) and text.strip() else None
    return None


def generic_captions(story: dict | None) -> set[str]:
    """Caption strings that describe a *day* rather than a photograph.

    A model asked for 398 captions will fill the ones it cannot see with a template -- on one real
    trip, 350 of 398 read "A moment from <place> during our <day title> day", one string covering
    up to 32 different photographs. Burned onto each of them it says nothing and buries the picture.

    Two signals, both independent of English phrasing:

      * **Reuse.** A caption attached to more than one asset is not a caption of any of them. This
        caught 342 of the 350 with no false positives -- no real description was ever shared.
      * **Restating the day.** A caption containing its day's own title verbatim is derived from
        the day, not from the frame. This catches the remainder, where a day held a single photo so
        the template appeared only once.
    """
    records = (story or {}).get("captions") or []
    counts: dict[str, int] = {}
    for record in records:
        caption = (record.get("caption") or "").strip()
        if caption:
            counts[caption] = counts.get(caption, 0) + 1

    titles = [
        (d.get("title") or "").strip().lower()
        for d in (story or {}).get("days") or []
        if (d.get("title") or "").strip()
    ]
    generic = {caption for caption, n in counts.items() if n > 1}
    for caption in counts:
        lowered = caption.lower()
        if any(title in lowered for title in titles):
            generic.add(caption)
    return generic


def _captions_by_asset(story: dict | None, *, drop_generic: bool = True) -> dict[str, dict]:
    skip = generic_captions(story) if drop_generic else set()
    out: dict[str, dict] = {}
    for record in (story or {}).get("captions") or []:
        asset_id = record.get("asset_id")
        caption = (record.get("caption") or "").strip()
        if asset_id and caption and caption not in skip:
            out[asset_id] = record
    return out


def _days_by_date(story: dict | None) -> dict[str, dict]:
    return {d["date"]: d for d in (story or {}).get("days") or [] if d.get("date")}


def build_cues(
    segments: list,
    offsets: list[float],
    story: dict | None,
    language: str,
    *,
    include_captions: bool = True,
    drop_generic_captions: bool = True,
) -> SubtitleTrack:
    """Timed cues for `language`, one per title card and (optionally) per captioned photograph.

    `segments` and `offsets` are parallel: `offsets[i]` is when `segments[i]` starts in the
    finished reel. Both come from the reel plan, so the subtitles cannot drift from the picture.
    """
    native = language.lower() == source_language(story)
    captions = _captions_by_asset(story, drop_generic=drop_generic_captions)
    days = _days_by_date(story)
    story = story or {}

    raw: list[Cue] = []
    for segment, start in zip(segments, offsets, strict=True):
        text, translated = None, native

        if segment.kind == "title":
            if segment.day is None:
                original = story.get("title") or segment.title
                rendered = original if native else _translated_field(story, language, "title")
                subtitle_original = story.get("subtitle") or segment.subtitle
                subtitle = (
                    subtitle_original if native else _translated_field(story, language, "subtitle")
                )
                if rendered is None:
                    rendered, translated = original, False
                    subtitle = subtitle_original
                else:
                    translated = True
                text = "\n".join(p for p in (rendered, subtitle) if p)
            else:
                day = days.get(segment.day, {})
                original = day.get("title") or segment.title
                rendered = original if native else _translated_field(day, language, "title")
                if rendered is None:
                    rendered, translated = original, False
                else:
                    translated = True
                text = rendered

        elif segment.kind == "end":
            # Its text is already burned into the card, in the reel's own language. A cue would
            # repeat it and, having no translation, would drag a track below 100%.
            text = None

        elif include_captions and segment.asset_id:
            record = captions.get(segment.asset_id)
            if record is not None:
                original = record["caption"]
                rendered = original if native else _translation(record, language)
                if rendered is None:
                    rendered, translated = original, False
                else:
                    translated = True
                text = rendered

        if text:
            raw.append(Cue(start, start + segment.seconds, text.strip(), translated))

    return SubtitleTrack(language=language, cues=_clamp(raw))


def _clamp(cues: list[Cue]) -> list[Cue]:
    """Stop each cue before the next one begins.

    Segments overlap by the crossfade, so the raw spans do too -- and two overlapping VTT cues
    render stacked on screen rather than replacing one another.
    """
    ordered = sorted(cues, key=lambda c: c.start)
    out: list[Cue] = []
    for index, cue in enumerate(ordered):
        end = cue.end
        if index + 1 < len(ordered):
            end = min(end, ordered[index + 1].start)
        if end > cue.start:
            out.append(Cue(cue.start, end, cue.text, cue.translated))
    return out


def _stamp(seconds: float, *, comma: bool = False) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    whole, millis = divmod(round(secs * 1000), 1000)
    separator = "," if comma else "."
    return f"{int(hours):02d}:{int(minutes):02d}:{int(whole):02d}{separator}{millis:03d}"


def to_webvtt(track: SubtitleTrack) -> str:
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(track.cues, start=1):
        lines += [str(index), f"{_stamp(cue.start)} --> {_stamp(cue.end)}", cue.text, ""]
    return "\n".join(lines)


def to_srt(track: SubtitleTrack) -> str:
    lines: list[str] = []
    for index, cue in enumerate(track.cues, start=1):
        lines += [
            str(index),
            f"{_stamp(cue.start, comma=True)} --> {_stamp(cue.end, comma=True)}",
            cue.text,
            "",
        ]
    return "\n".join(lines)


def write_track(track: SubtitleTrack, directory: Path, stem: str) -> Path:
    target = directory / f"{stem}.{track.language}.vtt"
    target.write_text(to_webvtt(track), encoding="utf-8")
    return target


CUE_BOTTOM_MARGIN_FRACTION = 0.07
CUE_TEXT_WIDTH_FRACTION = 0.86
CUE_FONT_HEIGHT_DIVISOR = 26
CUE_COLOR = (255, 255, 255, 255)
CUE_OUTLINE = (0, 0, 0, 230)
"""A stroke rather than a background box: subtitles sit over photographs of every brightness, and
an outline stays legible on both without covering the picture."""


MIN_CUE_FONT_PX = 12
SUBTITLE_SCALE_RANGE = (0.2, 5.0)

MIN_SHRINK_FRACTION = 0.55
"""How far a cue may shrink to stay on one line, as a fraction of the configured size.

A wrapped subtitle covers twice as much of the picture and reads worse, and Chinese wraps sooner
than English because each glyph is full-width. Below this the text is small enough that wrapping
is the lesser evil, so it wraps instead."""


def cue_font_size(height: int, scale: float = 1.0) -> int:
    """Burned-in subtitle size in pixels: a fraction of frame height, times the user's scale."""
    return max(MIN_CUE_FONT_PX, int(round(height / CUE_FONT_HEIGHT_DIVISOR * scale)))


def render_cue_images(
    track: SubtitleTrack,
    width: int,
    height: int,
    directory: Path,
    *,
    scale: float = 1.0,
    bottom_margin: float = CUE_BOTTOM_MARGIN_FRACTION,
) -> list[tuple[Cue, Path]]:
    """One transparent full-frame PNG per cue, text bottom-centred with an outline.

    Full-frame rather than a cropped strip so ffmpeg can composite at `0:0` and the vertical
    placement is decided here, in the code that measured the text.
    """
    from PIL import Image, ImageDraw  # local: keeps the module importable without Pillow

    directory.mkdir(parents=True, exist_ok=True)
    nominal = cue_font_size(height, scale)
    floor = max(MIN_CUE_FONT_PX, int(nominal * MIN_SHRINK_FRACTION))
    made: list[tuple[Cue, Path]] = []

    for index, cue in enumerate(track.cues):
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        max_width = int(width * CUE_TEXT_WIDTH_FRACTION)

        size = _one_line_size(draw, cue.text, max_width, nominal, floor)
        font = font_for(cue.text, size)
        text = renderable(cue.text, font)
        lines = _wrap_lines(draw, text, font, max_width)
        line_height = int(size * 1.35)
        bottom = height - int(height * bottom_margin)
        # A large scale on a long caption can push the block off the top of the frame. Text that
        # covers more of the picture is a choice the user made; text nobody can read is a bug.
        top = max(0, bottom - line_height * len(lines))

        for line_index, line in enumerate(lines):
            span = draw.textlength(line, font=font)
            draw.text(
                ((width - span) / 2, top + line_index * line_height),
                line,
                font=font,
                fill=CUE_COLOR,
                stroke_width=max(2, size // 12),
                stroke_fill=CUE_OUTLINE,
            )

        target = directory / f"cue{index:04d}.png"
        image.save(target)
        made.append((cue, target))
    return made


def _one_line_size(draw, text: str, max_width: int, nominal: int, floor: int) -> int:
    """Largest size at or below `nominal` that keeps every paragraph on one line.

    Falls back to `floor` when even that will not fit, and the caller then wraps -- unreadable text
    is worse than a second line. Each paragraph is measured separately because a cue may
    legitimately be two strings (a title and its subtitle), which should stay two lines.
    """
    paragraphs = [p for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return nominal
    for size in range(nominal, floor - 1, -1):
        font = font_for(text, size)
        drawable = renderable(text, font)
        if all(
            draw.textlength(paragraph, font=font) <= max_width
            for paragraph in drawable.split("\n")
            if paragraph.strip()
        ):
            return size
    return floor


def _wrap_lines(draw, text: str, font, max_width: int) -> list[str]:
    """Wrap on spaces, and on characters when there are none -- CJK has no word spaces."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if " " in paragraph:
            current = ""
            for word in paragraph.split():
                candidate = f"{current} {word}".strip()
                if draw.textlength(candidate, font=font) <= max_width or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
        else:
            current = ""
            for char in paragraph:
                if draw.textlength(current + char, font=font) <= max_width or not current:
                    current += char
                else:
                    lines.append(current)
                    current = char
            if current:
                lines.append(current)
    return lines or [""]


def burn_in(
    video: Path,
    cue_images: list[tuple[Cue, Path]],
    target: Path,
    *,
    preset: str = "veryfast",
    crf: int = 20,
) -> bool:
    """Composite the cue images onto `video`, writing `target`. False if ffmpeg fails.

    Re-encodes the picture, so it is slower and lossier than a soft track and writes a *separate*
    file -- the clean reel is never overwritten. Audio is stream-copied.

    Uses `overlay` with a time `enable` expression rather than the `subtitles` filter, which needs
    an ffmpeg built with libass. A stock Homebrew build has no such filter, and the tool must work
    on one.
    """
    if not cue_images:
        return False

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
    for _, path in cue_images:
        command += ["-i", str(path)]

    parts, label = [], "[0:v]"
    for index, (cue, _) in enumerate(cue_images, start=1):
        out = f"[v{index}]"
        # `repeatlast` is on by default, so a single-frame image persists; `enable` decides when it
        # is actually drawn.
        parts.append(
            f"{label}[{index}:v]overlay=0:0:enable='between(t,{cue.start:.3f},{cue.end:.3f})'{out}"
        )
        label = out
    parts.append(f"{label}format=yuv420p[vout]")

    command += [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "copy", "-movflags", "+faststart",
        str(target),
    ]  # fmt: skip

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("subtitle burn-in failed: %s", exc)
        return False
    if result.returncode != 0 or not target.exists():
        logger.warning("subtitle burn-in failed: %s", result.stderr[-400:])
        target.unlink(missing_ok=True)
        return False
    return True


def mux_subtitles(video: Path, tracks: list[tuple[str, Path]]) -> bool:
    """Add `tracks` to `video` as selectable `mov_text` streams. False if ffmpeg declines.

    A stream copy, so it costs a second or two and never re-encodes the picture. Failure is not
    fatal: the `.vtt` files stand on their own, and every player can load them beside the video.
    """
    if not tracks:
        return True
    target = video.with_suffix(".subtitled.mp4")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
    for _, path in tracks:
        command += ["-i", str(path)]
    # Video and audio explicitly, never `-map 0`. That would copy any subtitle stream the input
    # already has, so re-rendering a reel under the same name *appended* a second track instead of
    # replacing the first -- and the stale one carries no language tag, so a player shows a
    # nameless duplicate.
    command += ["-map", "0:v", "-map", "0:a?"]
    for index in range(len(tracks)):
        command += ["-map", f"{index + 1}:s"]
    # `+faststart` again, not just on the render. This mux rewrites the container and replaces
    # the video in place, so without it the moov atom lands at the end and a reel built with
    # --subtitles cannot start playing until the whole file has been fetched. Invisible to a
    # local player, which seeks; a multi-second stall when the file is served over HTTP.
    command += ["-c", "copy", "-c:s", "mov_text", "-movflags", "+faststart"]
    for index, (language, _) in enumerate(tracks):
        code = ISO_639_2.get(language.lower(), language.lower())
        command += [f"-metadata:s:s:{index}", f"language={code}"]
    command.append(str(target))

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("subtitle mux failed: %s", exc)
        return False
    if result.returncode != 0 or not target.exists():
        logger.warning("subtitle mux failed: %s", result.stderr[-300:])
        target.unlink(missing_ok=True)
        return False
    target.replace(video)
    return True
