# syntax=docker/dockerfile:1
# Production image: non-root, no build tools, dependencies from uv.lock only.
#   docker build --build-arg REVISION="$(git rev-parse --short=12 HEAD)" -t arbitron:local .
# Base images are pinned by version and digest; bump both together.

ARG PYTHON_IMAGE=python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first, so code changes reuse this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 arbitron \
    && useradd --system --uid 10001 --gid arbitron --no-create-home \
       --shell /usr/sbin/nologin arbitron
COPY --from=build /opt/venv /opt/venv
ARG REVISION=unknown
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARBITRON_BUILD_REVISION=${REVISION}
LABEL org.opencontainers.image.title="arbitron" \
      org.opencontainers.image.revision="${REVISION}"
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["arbitron"]
CMD ["--help"]
