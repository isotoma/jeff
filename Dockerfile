# syntax=docker/dockerfile:1.7

# --- build stage -------------------------------------------------------------
# uv sync in a slim Python image, then build the wheel. Keeps the final image
# free of build tooling and the uv cache.

FROM python:3.13-slim AS builder

ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_DEFAULT_INDEX=https://pypi.org/simple

COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml ./

RUN --mount=type=cache,target=/tmp/uv-cache \
    uv sync --no-install-project --no-dev

COPY src/ src/

RUN --mount=type=cache,target=/tmp/uv-cache \
    uv sync --no-dev

# --- runtime stage -----------------------------------------------------------

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    JEFF_HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml ./

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Mount checkpoints at /app/checkpoints via a volume / PV.
# JEFF_CHECKPOINT defaults to that path; override at deploy time if needed.
ENV JEFF_CHECKPOINT=/app/checkpoints/jeff-0.8b

CMD ["jeff-serve"]
