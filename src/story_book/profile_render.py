"""Rendering for the profile report. Split from the analysis so the numbers stay testable."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from story_book.profile import Profile, suggestions, warnings


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m {secs}s"


def render(profile: Profile, console: Console) -> None:
    console.print(f"\n[bold]{profile.source}[/]")

    _render_media(profile, console)
    _render_devices(profile, console)
    _render_time(profile, console)
    _render_warnings(profile, console)
    _render_suggestions(profile, console)


def _render_media(profile: Profile, console: Console) -> None:
    table = Table(title="Media", title_justify="left", header_style="bold")
    table.add_column("")
    table.add_column("count", justify="right")
    table.add_column("", justify="right")

    table.add_row("images", f"{profile.images:,}", f"HEIC {profile.heic_share:.0%}")
    table.add_row(
        "videos",
        f"{profile.videos:,}",
        human_duration(profile.video_seconds) if profile.videos else "",
    )
    table.add_row("total", f"{profile.total:,}", human_bytes(profile.total_bytes))
    if profile.ignored_files:
        table.add_row("[dim]skipped[/]", f"[dim]{profile.ignored_files:,}[/]", "[dim]non-media[/]")
    console.print(table)

    if profile.extensions:
        listed = ", ".join(
            f"{ext or '(none)'} {count:,}" for ext, count in profile.extensions.most_common()
        )
        console.print(f"  [dim]{listed}[/]\n")


def _render_devices(profile: Profile, console: Console) -> None:
    if not profile.devices:
        return
    table = Table(title="Devices", title_justify="left", header_style="bold")
    table.add_column("device")
    table.add_column("items", justify="right")
    table.add_column("with GPS", justify="right")

    for device, count in profile.devices.most_common():
        with_gps = profile.device_gps.get(device, 0)
        share = with_gps / count if count else 0.0
        colour = "red" if share == 0 else ("yellow" if share < 0.9 else "green")
        table.add_row(device, f"{count:,}", f"[{colour}]{share:.0%}[/]")
    console.print(table)
    console.print()


def _render_time(profile: Profile, console: Console) -> None:
    table = Table(title="Time & location", title_justify="left", header_style="bold")
    table.add_column("")
    table.add_column("")

    if profile.first and profile.last:
        table.add_row(
            "range",
            f"{profile.first:%Y-%m-%d %H:%M} → {profile.last:%Y-%m-%d %H:%M}  "
            f"({profile.span_days} day span, {len(profile.local_dates)} dates with media)",
        )
    table.add_row("no timestamp", f"{profile.without_timestamp:,}")
    table.add_row("GPS coverage", f"{profile.gps_coverage:.0%}  ({profile.without_gps:,} missing)")
    if profile.offsets:
        offsets = ", ".join(f"{key} {count:,}" for key, count in profile.offsets.most_common())
        table.add_row("UTC offsets", offsets)
    table.add_row(
        "offset changes",
        f"{profile.timezone_crossings}"
        + ("  [yellow](day boundaries at risk)[/]" if profile.timezone_crossings else ""),
    )
    if profile.gaps.count:
        table.add_row(
            "inter-photo gaps",
            f"p50 {profile.gaps.p50:.0f}m  p75 {profile.gaps.p75:.0f}m  "
            f"p90 {profile.gaps.p90:.0f}m  p95 {profile.gaps.p95:.0f}m  "
            f"p99 {profile.gaps.p99:.0f}m  max {profile.gaps.largest / 60:.1f}h",
        )
    table.add_row("largest gap", f"{profile.largest_day_gap_days:.2f} days")
    table.add_row("00:00-04:00 items", f"{profile.late_night_items:,}")
    console.print(table)
    console.print()


def _render_warnings(profile: Profile, console: Console) -> None:
    found = warnings(profile)
    if not found:
        return
    console.print("[bold yellow]Warnings[/]")
    for message in found:
        console.print(f"  [yellow]•[/] {message}")
    console.print()


def _render_suggestions(profile: Profile, console: Console) -> None:
    found = suggestions(profile)
    if not found:
        return
    table = Table(
        title="Suggested config (observed, not guessed)",
        title_justify="left",
        header_style="bold",
    )
    table.add_column("key")
    table.add_column("value", justify="right")
    table.add_column("basis")
    for key, value, why in found:
        table.add_row(key, f"[bold]{value}[/]", f"[dim]{why}[/]")
    console.print(table)
    console.print(
        "  [dim]Copy these into config.toml. They replace the guessed defaults in "
        "config.example.toml.[/]\n"
    )
