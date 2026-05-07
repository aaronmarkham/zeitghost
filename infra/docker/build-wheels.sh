#!/usr/bin/env bash
###############################################################################
# Build private dependency wheels for Docker images.
#
# Builds spiritwriter-core from the sibling repo into wheels/ so Dockerfiles
# can install without PyPI access.
#
# Usage: ./build-wheels.sh
# Expects: ../../../spiritwriter-core to exist (sibling checkout)
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WHEELS_DIR="${SCRIPT_DIR}/wheels"
SW_CORE="${SCRIPT_DIR}/../../../spiritwriter-core"

if [ ! -d "$SW_CORE" ]; then
    echo "ERROR: spiritwriter-core not found at ${SW_CORE}"
    echo "Clone it as a sibling: git clone <url> alongside zeitghost/"
    exit 1
fi

mkdir -p "$WHEELS_DIR"
rm -f "$WHEELS_DIR"/spiritwriter_core-*.whl

echo "Building spiritwriter-core wheel..."
pip wheel "$SW_CORE" --no-deps -w "$WHEELS_DIR" 2>&1 | tail -3

echo ""
echo "Wheels ready in ${WHEELS_DIR}:"
ls -1 "$WHEELS_DIR"/*.whl
