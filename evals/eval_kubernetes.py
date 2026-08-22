"""Offline validation for the Kubernetes deployment bundle."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
K8S_DIR = ROOT / "k8s"


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl is not installed")
def test_kustomize_bundle_renders_without_a_cluster():
    result = subprocess.run(
        ["kubectl", "kustomize", str(K8S_DIR)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = result.stdout
    for resource in (
        "kind: Namespace",
        "kind: ConfigMap",
        "kind: StatefulSet",
        "kind: Deployment",
        "name: janus-api",
        "name: janus-worker",
        "name: janus-postgres",
    ):
        assert resource in rendered


def test_worker_manifest_preserves_docker_socket_boundary():
    worker = (K8S_DIR / "worker.yaml").read_text(encoding="utf-8")

    assert "path: /var/run/docker.sock" in worker
    assert 'nodeSelector:\n        janus.io/docker-sandbox: "true"' in worker
    assert "automountServiceAccountToken: false" in worker
    assert "runAsNonRoot: true" in worker
    assert "supplementalGroups: []" in worker


def test_runtime_image_and_api_gate_wiring_is_explicit():
    config = (K8S_DIR / "configmap.yaml").read_text(encoding="utf-8")
    api = (K8S_DIR / "api.yaml").read_text(encoding="utf-8")

    assert "SANDBOX_IMAGE: ghcr.io/codebyd3v/janus/sandbox:replace-with-git-sha" in config
    assert 'name: USE_CONTAINERIZED_GATE\n              value: "false"' in api


def test_migrations_have_separate_ordered_entry_points():
    kustomization = (K8S_DIR / "kustomization.yaml").read_text(encoding="utf-8")
    deploy = (K8S_DIR / "deploy.sh").read_text(encoding="utf-8")

    assert "migration-job.yaml" not in kustomization
    assert "rollout status statefulset/janus-postgres" in deploy
    assert "wait --for=condition=complete job/janus-migrate" in deploy
    assert deploy.index("wait --for=condition=complete job/janus-migrate") < deploy.index('kubectl apply -k "$TMP_DIR/k8s"')


def test_secret_template_is_not_applied_by_default_kustomization():
    kustomization = (K8S_DIR / "kustomization.yaml").read_text(encoding="utf-8")

    assert "\n  - secret.example.yaml\n" not in kustomization
    assert "janus-runtime-secrets" in (K8S_DIR / "api.yaml").read_text(encoding="utf-8")
