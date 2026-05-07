#!/usr/bin/env bash
###############################################################################
# Zeitghost builder loop — ingest new articles, regenerate site.
# Dedup via shard store means only new articles hit the Claude API.
###############################################################################
set -euo pipefail

INTERVAL="${ZEITGHOST_INTERVAL:-3600}"
OUTPUT="${ZEITGHOST_OUTPUT:-/output}"
FEEDS="${ZEITGHOST_FEEDS:-feeds/newsapi.yaml}"
INGEST_LIMIT="${ZEITGHOST_INGEST_LIMIT:-50}"
MAX_REQUESTS="${ZEITGHOST_MAX_REQUESTS:-2}"

INGEST_ARGS=(--feeds "$FEEDS")
if [[ "$INGEST_LIMIT" != "0" ]]; then
    INGEST_ARGS+=(--limit "$INGEST_LIMIT")
fi
if [[ -n "$MAX_REQUESTS" ]]; then
    INGEST_ARGS+=(--max-requests "$MAX_REQUESTS")
fi

echo "Zeitghost builder starting (interval: ${INTERVAL}s)"

# Generate initial site immediately so nginx healthcheck has something to serve
python -m zeitghost.cli build --output "$OUTPUT" || true

while true; do
    echo "$(date -Iseconds) Ingesting..."
    python -m zeitghost.cli ingest "${INGEST_ARGS[@]}" \
        || echo "Ingest failed, will retry next cycle"
    echo "$(date -Iseconds) Building..."
    python -m zeitghost.cli build --output "$OUTPUT" \
        || echo "Build failed, will retry next cycle"
    echo "$(date -Iseconds) Sleeping ${INTERVAL}s"
    sleep "$INTERVAL"
done
