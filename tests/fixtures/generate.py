"""Regenerate the committed test fixtures.

Run with `uv run python tests/fixtures/generate.py`. The outputs are committed, so this only
needs re-running when a new case is added. Every file is kept tiny (a few KB) so the repo
stays cheap to clone.

Video fixtures need ffmpeg; without it they are skipped and the corresponding tests skip too.
"""

from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import piexif
from PIL import Image, ImageDraw, ImageFilter

FIXTURE_DIR = Path(__file__).parent
MEDIA_DIR = FIXTURE_DIR / "media"

# Salzburg / Vienna, matching the plan doc's running example.
SALZBURG = (47.8095, 13.0550)
VIENNA = (48.2082, 16.3738)


def deg_to_dms_rational(value: float) -> tuple[tuple[int, int], ...]:
    value = abs(value)
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = Fraction(minutes_float - minutes).limit_denominator(10000) * 60
    return (
        (degrees, 1),
        (minutes, 1),
        (seconds.numerator, seconds.denominator),
    )


def gps_ifd(lat: float, lon: float) -> dict:
    return {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: deg_to_dms_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: deg_to_dms_rational(lon),
        piexif.GPSIFD.GPSAltitudeRef: 0,
        piexif.GPSIFD.GPSAltitude: (424, 1),
    }


def exif_bytes(
    *,
    taken: str,
    make: str,
    model: str,
    offset: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> bytes:
    zeroth = {
        piexif.ImageIFD.Make: make.encode(),
        piexif.ImageIFD.Model: model.encode(),
        piexif.ImageIFD.Orientation: 1,
    }
    exif = {
        piexif.ExifIFD.DateTimeOriginal: taken.encode(),
        piexif.ExifIFD.SubSecTimeOriginal: b"00",
    }
    if offset is not None:
        exif[piexif.ExifIFD.OffsetTimeOriginal] = offset.encode()
    gps = gps_ifd(lat, lon) if lat is not None and lon is not None else {}
    return piexif.dump({"0th": zeroth, "Exif": exif, "GPS": gps})


def scene(seed: int, size: tuple[int, int] = (320, 240), blur: float = 0.0) -> Image.Image:
    """A deterministic synthetic 'photo' -- distinct shapes so CLIP and pHash see real variety."""
    image = Image.new("RGB", size, (140 + seed * 7 % 80, 160, 200 - seed * 5 % 60))
    draw = ImageDraw.Draw(image)
    for i in range(6):
        offset = (seed * 13 + i * 29) % 90
        box = (10 + offset, 20 + i * 18, 90 + offset + i * 12, 80 + i * 20)
        draw.rectangle(box, fill=(30 + i * 30, 200 - i * 25, 60 + offset))
        draw.ellipse(
            (40 + offset, 60 + i * 10, 120 + offset, 140 + i * 8), outline=(255, 255, 255), width=3
        )
    draw.text((12, size[1] - 24), f"scene {seed}", fill=(255, 255, 0))
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    return image


def flat_image(color: tuple[int, int, int], size=(320, 240)) -> Image.Image:
    return Image.new("RGB", size, color)


def screenshot_image() -> Image.Image:
    """Flat UI-looking panel with crisp text: what the content classifier must reject."""
    image = Image.new("RGB", (360, 260), (250, 250, 252))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 360, 32), fill=(60, 90, 200))
    draw.text((10, 10), "Settings", fill=(255, 255, 255))
    for row in range(7):
        y = 48 + row * 28
        draw.rectangle((12, y, 348, y + 20), fill=(238, 238, 242))
        draw.text((20, y + 4), f"Option row {row}", fill=(40, 40, 40))
    return image


def receipt_image() -> Image.Image:
    image = Image.new("RGB", (240, 380), (252, 252, 248))
    draw = ImageDraw.Draw(image)
    draw.text((60, 14), "CAFE MOZART", fill=(20, 20, 20))
    draw.line((16, 34, 224, 34), fill=(120, 120, 120))
    for row in range(11):
        y = 46 + row * 26
        draw.text((20, y), f"Item {row}", fill=(30, 30, 30))
        draw.text((170, y), f"{row + 2}.50", fill=(30, 30, 30))
    draw.line((16, 340, 224, 340), fill=(120, 120, 120))
    draw.text((20, 350), "TOTAL     62.00", fill=(10, 10, 10))
    return image


def save_jpeg(image: Image.Image, path: Path, exif: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"quality": 72, "optimize": True}
    if exif is not None:
        kwargs["exif"] = exif
    image.save(path, "JPEG", **kwargs)


def save_heic(image: Image.Image, path: Path, exif: bytes | None = None) -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()
    kwargs = {"quality": 60}
    if exif is not None:
        kwargs["exif"] = exif
    image.save(path, "HEIF", **kwargs)


def make_video(path: Path, *, seconds: int, with_speech: bool) -> bool:
    """A tiny clip. 'Speech' is a voice-band tone burst -- enough to exercise VAD paths."""
    if not shutil.which("ffmpeg"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    audio = (
        f"sine=frequency=220:duration={seconds}"
        if with_speech
        else f"anullsrc=channel_layout=mono:sample_rate=16000:duration={seconds}"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=160x120:rate=10:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            audio,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "40",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "16k",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return True


def main() -> None:
    if MEDIA_DIR.exists():
        shutil.rmtree(MEDIA_DIR)
    MEDIA_DIR.mkdir(parents=True)

    # HEIC with GPS and an explicit UTC offset: the best-case metadata path.
    save_heic(
        scene(1),
        MEDIA_DIR / "heic_gps_offset.heic",
        exif_bytes(
            taken="2026:07:18 09:20:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )

    # JPEG with GPS but no offset tag: timezone must come from coordinates.
    save_jpeg(
        scene(2),
        MEDIA_DIR / "jpeg_gps_no_offset.jpg",
        exif_bytes(
            taken="2026:07:18 11:45:00",
            make="Apple",
            model="iPhone 16 Pro",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )

    # No GPS at all, different device: needs GPS backfill from a time-adjacent neighbor.
    save_jpeg(
        scene(3),
        MEDIA_DIR / "jpeg_no_gps.jpg",
        exif_bytes(taken="2026:07:18 12:05:00", make="Sony", model="ILCE-7M4"),
    )

    # No EXIF whatsoever: must degrade, never crash.
    save_jpeg(scene(4), MEDIA_DIR / "jpeg_no_exif.jpg")

    # Burst pair: seconds apart, near-identical frames.
    burst = scene(5)
    save_jpeg(
        burst,
        MEDIA_DIR / "burst_a.jpg",
        exif_bytes(
            taken="2026:07:18 15:30:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )
    save_jpeg(
        burst.filter(ImageFilter.GaussianBlur(0.4)),
        MEDIA_DIR / "burst_b.jpg",
        exif_bytes(
            taken="2026:07:18 15:30:02",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )

    # Exact duplicate: same bytes, different filename. Content hashing must collapse these.
    exact = MEDIA_DIR / "exact_a.jpg"
    save_jpeg(
        scene(6),
        exact,
        exif_bytes(
            taken="2026:07:18 16:00:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )
    shutil.copy2(exact, MEDIA_DIR / "exact_b.jpg")

    # Two visually distinct photos that must never merge into one cluster.
    save_jpeg(
        scene(7),
        MEDIA_DIR / "distinct_a.jpg",
        exif_bytes(
            taken="2026:07:18 17:10:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )
    save_jpeg(
        scene(42),
        MEDIA_DIR / "distinct_b.jpg",
        exif_bytes(
            taken="2026:07:18 17:12:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )

    # Sharp vs blurred version of the same scene: the quality-scoring ordering test.
    save_jpeg(
        scene(8),
        MEDIA_DIR / "sharp.jpg",
        exif_bytes(
            taken="2026:07:18 18:00:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )
    save_jpeg(
        scene(8, blur=3.5),
        MEDIA_DIR / "blurred.jpg",
        exif_bytes(
            taken="2026:07:18 18:00:04",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )

    # Content classes the pipeline must keep out of highlights.
    save_jpeg(
        screenshot_image(),
        MEDIA_DIR / "screenshot.jpg",
        exif_bytes(taken="2026:07:18 19:00:00", make="Apple", model="iPhone 16 Pro"),
    )
    save_jpeg(
        receipt_image(),
        MEDIA_DIR / "receipt.jpg",
        exif_bytes(
            taken="2026:07:18 20:30:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
        ),
    )

    # Over/under exposed: histogram clipping at both ends.
    save_jpeg(flat_image((250, 250, 250)), MEDIA_DIR / "overexposed.jpg")
    save_jpeg(flat_image((6, 6, 8)), MEDIA_DIR / "underexposed.jpg")

    # Timezone crossing: same clock hour, two zones, an hour apart in real time.
    # Vienna +02:00 at 23:30 local, then Istanbul +03:00 at 00:30 local the next day.
    save_jpeg(
        scene(9),
        MEDIA_DIR / "tz_before.jpg",
        exif_bytes(
            taken="2026:07:19 23:30:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+02:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
        ),
    )
    save_jpeg(
        scene(10),
        MEDIA_DIR / "tz_after.jpg",
        exif_bytes(
            taken="2026:07:20 00:30:00",
            make="Apple",
            model="iPhone 16 Pro",
            offset="+03:00",
            lat=41.0082,
            lon=28.9784,
        ),
    )

    # A file the scanner must ignore.
    (MEDIA_DIR / "notes.txt").write_text("not media\n")

    made_speech = make_video(MEDIA_DIR / "clip_speech.mov", seconds=3, with_speech=True)
    made_silent = make_video(MEDIA_DIR / "clip_silent.mp4", seconds=3, with_speech=False)
    if not (made_speech and made_silent):
        print("ffmpeg not found -- video fixtures skipped (`brew install ffmpeg` to add them)")

    total = sum(p.stat().st_size for p in MEDIA_DIR.iterdir())
    print(f"wrote {len(list(MEDIA_DIR.iterdir()))} fixtures, {total / 1024:.0f} KB total")


if __name__ == "__main__":
    main()
