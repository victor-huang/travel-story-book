# The service and the CLI it runs ship as one image.
#
# Build from the *repository root*, because the service depends on the pipeline as a path
# dependency and the pipeline's source has to be in the context:
#
#     docker build -f service/Dockerfile -t storybook-service .
#
# A container is the deployment unit specifically so the hosting target stays an open question: the
# same image runs on a VM under compose, on Fly machines, on ECS or on Cloud Run. What it cannot do
# is run in a request-scoped serverless runtime -- a build is minutes to hours and wants a real
# filesystem with tens of gigabytes on it.

FROM python:3.12-slim-bookworm

# Pinned exactly. `latest` tells you the version, not which refs resolve.
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /usr/local/bin/

# exiftool and ffmpeg are the two binaries the pipeline treats as system dependencies; without the
# first, every timestamp, day boundary and timezone is lost.
#
# fonts-noto-cjk is not optional either, and the reason is a shipped bug: text drawn into images
# picks a font by what has to be drawn, and with no CJK face installed a burn-in is *correctly*
# skipped -- so a reel with Chinese subtitles would come back silently without them. This is the
# same package `ci.yml` installs on Linux, for the same reason.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        ffmpeg \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# The pipeline first: its sources change far less often than the service's, so the dependency
# resolve is cached across service edits.
COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src
COPY service/pyproject.toml service/uv.lock /app/service/

WORKDIR /app/service
RUN uv sync --frozen --no-install-project

COPY service/storybook_service /app/service/storybook_service
RUN uv sync --frozen

# These are someone's family photographs. Nothing here needs root.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /var/lib/storybook \
    && chown -R app:app /var/lib/storybook
USER app

ENV STORY_SERVICE_DATA_ROOT=/var/lib/storybook
EXPOSE 8000

# Liveness only -- /ready is the dependency check, and a platform that restarts on it would loop
# forever on a broken image instead of reporting one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "storybook_service.app:app", "--host", "0.0.0.0", "--port", "8000"]
