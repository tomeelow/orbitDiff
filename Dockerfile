# The Python services. One image for the API, scheduler and migrations; the worker builds
# the same file with SYNC_ARGS="--group ml" for the cloud model (CPU-only PyTorch), so the
# API image stays small. Data, secrets and model weights come in at run time, never at build
# time.
FROM ghcr.io/astral-sh/uv:0.11.20 AS uv

FROM python:3.12.16-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

# rasterio's wheel bundles GDAL but links the system's expat.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

ARG SYNC_ARGS=""
# Dependencies first, so a code change doesn't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project $SYNC_ARGS
COPY pyproject.toml uv.lock alembic.ini ./
COPY config config
COPY migrations migrations
COPY src src
# Editable, as in development: the package finds config/ and data/ under /app.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev $SYNC_ARGS

RUN useradd --create-home --uid 1000 orbitdiff
USER orbitdiff
