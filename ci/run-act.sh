#!/usr/bin/env bash
# Invoke from the candidate checkout. The cluster already contains Stage D.
set -euo pipefail
CLUSTER=${1:?Usage: bash ci/run-act.sh <existing-kind-cluster>}
[[ "$CLUSTER" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo 'Invalid cluster name' >&2; exit 1; }
for tool in docker kind kubectl act uv; do command -v "$tool" >/dev/null; done
docker info >/dev/null
kind get clusters | grep -Fx "$CLUSTER" >/dev/null
docker image inspect agent-relay-act:local >/dev/null
# A new UUID is used even when act supplies the same github.run_id repeatedly.
RUN_VALUE=$(UV_CACHE_DIR=/tmp/relay-act-uv-cache UV_PYTHON_DOWNLOADS=never uv run --no-project python -c 'import uuid; print(uuid.uuid4().hex)')
RELAY_ACT_DIR=$(mktemp -d /tmp/agent-relay-act.XXXXXX)
RELAY_ACT_LOCK="/tmp/agent-relay-act-${CLUSTER}.lock"
mkdir "$RELAY_ACT_LOCK" || { echo 'Another local workflow is using this cluster' >&2; exit 1; }
trap 'rm -f "$RELAY_ACT_DIR/kubeconfig"; rmdir "$RELAY_ACT_DIR"; rmdir "$RELAY_ACT_LOCK"' EXIT
umask 077
kind get kubeconfig --name "$CLUSTER" --internal > "$RELAY_ACT_DIR/kubeconfig"
ARCH=$(docker info --format '{{.Architecture}}')
case "$ARCH" in aarch64|arm64) ARCH=arm64 ;; x86_64|amd64) ARCH=amd64 ;; *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;; esac
echo "Local workflow: cluster=$CLUSTER run=$RUN_VALUE commit=$(git rev-parse HEAD)"
XDG_CACHE_HOME=/tmp/agent-relay-act-cache act workflow_dispatch -W .github/workflows/ci.yml -j local-kind \
  -P ubuntu-latest=agent-relay-act:local --pull=false \
  --container-architecture "linux/$ARCH" --container-daemon-socket /var/run/docker.sock \
  --network kind --container-options "--volume $RELAY_ACT_DIR/kubeconfig:/run/relay/kubeconfig:ro" \
  --env KUBECONFIG=/run/relay/kubeconfig --env RELAY_KIND_CLUSTER="$CLUSTER" \
  --env RELAY_CI_RUN="$RUN_VALUE" --action-cache-path /tmp/agent-relay-act-actions \
  --no-cache-server --rm
