# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl make \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml Makefile tailwind.config.js ./
COPY app/ app/
COPY vendor/ vendor/

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install "openapi-python-client>=0.29.1,<0.30"

RUN make generate PYTHON=python
RUN --mount=type=cache,target=/root/.cache/tailwind \
    make css TAILWIND_ARCH="$TARGETARCH" TAILWIND_CACHE_DIR=/root/.cache/tailwind
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 tagger \
 && useradd --uid 10001 --gid 10001 --no-create-home tagger \
 && install -d -o 10001 -g 10001 -m 0750 /data

RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    python -m pip install --no-index --find-links /wheels immich-llm-tagger

ENV DATA_DIR=/data
WORKDIR /srv
USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).close()"

CMD ["uvicorn", "app.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
