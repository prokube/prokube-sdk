#!/usr/bin/env bash
# Regenerates the vendored ate API (Substrate control plane) stubs in
# src/prokube/sandbox/_ateapi/ from the pinned upstream proto.
set -euo pipefail

# kagent fork of Substrate, tag v0.6.0-alpha2: the version the platform installs.
SUBSTRATE_REPO="kagent-dev/substrate"
SUBSTRATE_COMMIT="4df1cd30f54e7efd277252e6372ccbd68fdad9c2"
# Keep in step with the grpcio and protobuf floors in pyproject.toml.
GRPCIO_TOOLS_VERSION="1.84.0"

ROOT=$(git rev-parse --show-toplevel)
OUT="$ROOT/src/prokube/sandbox/_ateapi"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

curl -fsSL -o "$TMP/ateapi.proto" \
  "https://raw.githubusercontent.com/$SUBSTRATE_REPO/$SUBSTRATE_COMMIT/pkg/proto/ateapipb/ateapi.proto"

uv run --no-project --with "grpcio-tools==$GRPCIO_TOOLS_VERSION" \
  python -m grpc_tools.protoc -I "$TMP" \
  --python_out="$OUT" --pyi_out="$OUT" --grpc_python_out="$OUT" \
  "$TMP/ateapi.proto"

# protoc emits a top-level import; make it relative to the package.
sed -i.bak 's/^import ateapi_pb2 as/from . import ateapi_pb2 as/' "$OUT/ateapi_pb2_grpc.py"
rm "$OUT/ateapi_pb2_grpc.py.bak"

echo "$SUBSTRATE_REPO@$SUBSTRATE_COMMIT" > "$OUT/SOURCE"
