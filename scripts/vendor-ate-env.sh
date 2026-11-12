#!/usr/bin/env bash
# Vendors the upstream env client (agent-substrate/env, clients/python) into
# src/prokube/sandbox/_ate_env/ at the pinned commit, with its gRPC stubs
# regenerated for the protobuf 6 runtime that kfp and mlflow need.
set -euo pipefail

# Same commit as the env guest in the coding runtime image (prokube/sandbox-images#16).
ENV_REPO="agent-substrate/env"
ENV_COMMIT="7be7449433f5549f02404b0b85e75d3fa686ea7e"
# Same as in gen-ateapi.sh.
GRPCIO_TOOLS_VERSION="1.80.0"

ROOT=$(git rev-parse --show-toplevel)
OUT="$ROOT/src/prokube/sandbox/_ate_env"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

curl -fsSL "https://codeload.github.com/$ENV_REPO/tar.gz/$ENV_COMMIT" | tar -xz -C "$TMP"
SRC="$TMP/env-$ENV_COMMIT"

rm -rf "$OUT"
cp -R "$SRC/clients/python/src/ate_env" "$OUT"
cp "$SRC/LICENSE" "$OUT/LICENSE"
find "$OUT" -name '__pycache__' -prune -exec rm -rf {} +

GEN="$OUT/_gen"
rm -f "$GEN"/ateenv/v1alpha/*_pb2*.py "$GEN"/ateenv/v1alpha/*_pb2*.pyi
uv run --no-project --with "grpcio-tools==$GRPCIO_TOOLS_VERSION" \
  python -m grpc_tools.protoc -I "$SRC/proto" \
  --python_out="$GEN" --pyi_out="$GEN" --grpc_python_out="$GEN" \
  "$SRC/proto/ateenv/v1alpha/env.proto" "$SRC/proto/ateenv/v1alpha/guest.proto"

# protoc emits absolute imports; make them relative to the package, as upstream does.
for f in "$GEN"/ateenv/v1alpha/*_pb2_grpc.py; do
  sed -i.bak 's/^from ateenv\.v1alpha import \(.*\)$/from . import \1/' "$f"
  rm "$f.bak"
done

echo "$ENV_REPO@$ENV_COMMIT (clients/python/src/ate_env)" > "$OUT/SOURCE"
