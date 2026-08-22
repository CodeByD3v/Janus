#!/usr/bin/env bash
# Ordered Kubernetes deployment for Janus.
# Prerequisites: kubectl context selected, janus-runtime-secrets created out of band,
# and a node pool labeled janus.io/docker-sandbox=true for the worker.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 IMAGE_TAG" >&2
  exit 2
fi

IMAGE_TAG="$1"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-ghcr.io/codebyd3v/janus}"
SERVICE_IMAGE="${IMAGE_REPOSITORY}/service:${IMAGE_TAG}"
SANDBOX_IMAGE="${IMAGE_REPOSITORY}/sandbox:${IMAGE_TAG}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Work on a temporary copy so release substitution never edits the repository.
cp -R "$ROOT_DIR/k8s" "$TMP_DIR/k8s"
find "$TMP_DIR/k8s" -name kustomization.yaml -type f -print0 | while IFS= read -r -d '' file; do
  sed -i "s#newName: ghcr.io/codebyd3v/janus/service#newName: ${IMAGE_REPOSITORY}/service#g" "$file"
  sed -i "s#newName: ghcr.io/codebyd3v/janus/sandbox#newName: ${IMAGE_REPOSITORY}/sandbox#g" "$file"
  sed -i "s#newTag: replace-with-git-sha#newTag: ${IMAGE_TAG}#g" "$file"
done
sed -i "s#SANDBOX_IMAGE: .*#SANDBOX_IMAGE: ${SANDBOX_IMAGE}#" "$TMP_DIR/k8s/configmap.yaml"
sed -i "s#image: janus-service#image: ${SERVICE_IMAGE}#g" "$TMP_DIR/k8s/migration-job.yaml"

# Apply only the infrastructure needed to make the database available. The
# root Kustomization intentionally excludes the Job to avoid accidental races.
kubectl apply -f "$TMP_DIR/k8s/namespace.yaml"
kubectl apply -f "$TMP_DIR/k8s/configmap.yaml"
kubectl apply -f "$TMP_DIR/k8s/storage.yaml"
kubectl apply -f "$TMP_DIR/k8s/postgres.yaml"
kubectl -n janus rollout status statefulset/janus-postgres --timeout=5m

kubectl -n janus delete job janus-migrate --ignore-not-found
kubectl apply -f "$TMP_DIR/k8s/migration-job.yaml"
kubectl -n janus wait --for=condition=complete job/janus-migrate --timeout=10m

kubectl apply -k "$TMP_DIR/k8s"
kubectl -n janus rollout status deployment/janus-api --timeout=10m
kubectl -n janus rollout status deployment/janus-worker --timeout=10m

echo "Janus ${IMAGE_TAG} deployed successfully. Service image: ${SERVICE_IMAGE}; sandbox image: ${SANDBOX_IMAGE}"
