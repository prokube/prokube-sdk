#!/usr/bin/env bash
# Regenerates the vendored ate API (Substrate control plane) stubs in
# src/prokube/sandbox/_ateapi/ from the pinned upstream proto.
set -euo pipefail

# kagent fork of Substrate, tag v1.0.0-rc1: the version the platform installs.
SUBSTRATE_REPO="kagent-dev/substrate"
SUBSTRATE_COMMIT="71b1f9ec80848f8fd46f5d5d7e6b84ba36a80966"
# Keep in step with the grpcio and protobuf floors in pyproject.toml.
GRPCIO_TOOLS_VERSION="1.84.0"

ROOT=$(git rev-parse --show-toplevel)
OUT="$ROOT/src/prokube/sandbox/_ateapi"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

curl -fsSL -o "$TMP/ateapi.proto" \
  "https://raw.githubusercontent.com/$SUBSTRATE_REPO/$SUBSTRATE_COMMIT/api/ateapi/v1/ateapi.proto"

uv run --no-project --with "grpcio-tools==$GRPCIO_TOOLS_VERSION" \
  python -m grpc_tools.protoc -I "$TMP" \
  --python_out="$OUT" --pyi_out="$OUT" --grpc_python_out="$OUT" \
  "$TMP/ateapi.proto"

# protoc emits a top-level import; make it relative to the package.
sed -i.bak 's/^import ateapi_pb2 as/from . import ateapi_pb2 as/' "$OUT/ateapi_pb2_grpc.py"
rm "$OUT/ateapi_pb2_grpc.py.bak"

echo "$SUBSTRATE_REPO@$SUBSTRATE_COMMIT" > "$OUT/SOURCE"
