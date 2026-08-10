"""Environment-driven settings.

Read from the environment, never from a checked-in file: the hosting target is still open (see the
iOS tracker's open questions), and every candidate injects configuration as environment variables.
No secret has a default here, and no secret belongs in this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "STORY_SERVICE_"


@dataclass(frozen=True)
class Settings:
    """What the skeleton needs. S02+ add fields; they do not add config files."""

    # Invoked as a subprocess, because that is how the service runs a build. Resolving the CLI by
    # name proves it is on PATH and executable, which importing `story_book` would not.
    story_book_bin: str = "story-book"

    # Trip working directories -- the CLI's `--out`. One POSIX filesystem with real space on it is
    # a hard requirement of the pipeline, and it is the property that rules out request-scoped
    # serverless runtimes.
    data_root: Path = Path("var/service-data")

    # A dependency probe that hangs must fail rather than hold the readiness endpoint open.
    probe_timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        return cls(
            story_book_bin=source.get(f"{ENV_PREFIX}STORY_BOOK_BIN", cls.story_book_bin),
            data_root=Path(source.get(f"{ENV_PREFIX}DATA_ROOT", str(cls.data_root))),
            probe_timeout_s=float(
                source.get(f"{ENV_PREFIX}PROBE_TIMEOUT_S", str(cls.probe_timeout_s))
            ),
        )
