###############################################################################
# Zeitghost News Builder
# Runs `zeitghost ingest && zeitghost build` on a loop, writing /output.
#
# Pre-req: run ./build-wheels.sh to build the spiritwriter wheel into wheels/
###############################################################################
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY infra/docker/wheels/ ./wheels/
RUN pip install --no-cache-dir --prefix=/install wheels/*.whl && \
    pip install --no-cache-dir --prefix=/install \
        "anthropic>=0.40.0" "requests>=2.31" "jinja2>=3.1" \
        "click>=8.1" "rich>=13.0" "pyyaml>=6.0" \
        "psycopg2-binary>=2.9" \
        "trafilatura>=2.0"

# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Git SHA of the source tree being built. Surfaced as ENV so generator
# can render it in the site footer + so `docker exec ... env` answers
# "what commit is actually deployed" without inference.
ARG COMMIT_SHA=unknown
ENV ZEITGHOST_COMMIT=$COMMIT_SHA

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash zeitghost && \
    mkdir -p /home/zeitghost/data/shards /output && \
    chown -R zeitghost:zeitghost /home/zeitghost /output
USER zeitghost
WORKDIR /home/zeitghost/app

COPY --from=builder /install /usr/local

COPY --chown=zeitghost:zeitghost zeitghost/ ./zeitghost/
COPY --chown=zeitghost:zeitghost feeds/ ./feeds/
COPY --chown=zeitghost:zeitghost templates/ ./templates/
COPY --chown=zeitghost:zeitghost static/ ./static/
COPY --chown=zeitghost:zeitghost scripts/ ./scripts/
COPY --chown=zeitghost:zeitghost pyproject.toml ./
COPY --chown=zeitghost:zeitghost infra/docker/entrypoint.sh ./entrypoint.sh

# Install the zeitghost package itself so the [project.scripts] entry
# point ("zeitghost = zeitghost.cli:main") lands on PATH. --no-deps
# because everything zeitghost depends on is already installed in the
# builder stage.
USER root
RUN pip install --no-cache-dir --no-deps .
USER zeitghost

ENV PYTHONUNBUFFERED=1 \
    ZEITGHOST_SHARD_STORE=/home/zeitghost/data/shards \
    ZEITGHOST_OUTPUT=/output \
    ZEITGHOST_INTERVAL=3600

ENTRYPOINT ["bash", "./entrypoint.sh"]
