# Kubernetes deployment path

This directory provides a Kubernetes deployment path for Janus alongside the
existing single-VM Docker Compose deployment. It is an explicit alternative,
not a claim that the Compose topology has been replaced. The manifests are
renderable offline; live cluster validation, storage-class validation, and
production rollout remain operator responsibilities.

## Deployment model

The API runs as a single-replica `Deployment` behind the `janus-api` `ClusterIP`
Service. The conservative replica count prevents assuming that Chroma's
persistent store is safe for concurrent API writers. A single worker runs the
queue consumer and mounts the host Docker socket so it can launch the
locked-down gate sandbox. PostgreSQL runs as a single-replica `StatefulSet` with
a persistent volume. ChromaDB and the GitHub repository cache use persistent
volume claims.

The worker's Docker-socket mount is a broad host-control boundary. Schedule it
only on a dedicated, tainted node pool labeled
`janus.io/docker-sandbox=true`, and do not place unrelated workloads on those
nodes. The worker is deliberately not horizontally scaled because multiple
workers must be evaluated against queue and database semantics before adding
replicas.

The API explicitly overrides `USE_CONTAINERIZED_GATE=false`; only the worker
invokes the Docker-isolated gate. The shared ConfigMap contains a full,
immutable `SANDBOX_IMAGE` placeholder because Kustomize image transforms do not
rewrite arbitrary ConfigMap strings. The release script substitutes the exact
service and sandbox image tags together before applying resources.

## Prerequisites

The cluster must provide an Ingress controller if GitHub webhooks or external
operator access are required, a TLS certificate for the public host, a
`ReadWriteMany` StorageClass for the Chroma and repository-cache claims, and a
node pool that can safely expose `/var/run/docker.sock`. The service image
includes the Docker CLI; the daemon remains on the dedicated worker node.

The worker pod runs as uid/gid `1000`. Docker socket permissions are
cluster-specific, so set `spec.template.spec.securityContext.supplementalGroups`
to the numeric group owning `/var/run/docker.sock` on the selected node pool.
Do not copy a Docker GID from this example to another cluster. If the host
socket uses an ACL that permits uid/gid 1000, the field may remain empty; verify
that explicitly before rollout.

Create the runtime secret through the cluster's secret manager or an
equivalent sealed-secret workflow. `secret.example.yaml` is a documentation
template and is intentionally not included in `kustomization.yaml`.

```bash
kubectl create namespace janus
kubectl -n janus create secret generic janus-runtime-secrets \
  --from-literal=POSTGRES_PASSWORD='<random-password>' \
  --from-literal=DATABASE_URL='postgresql+psycopg2://janus:<password>@janus-postgres:5432/janus' \
  --from-literal=GOOGLE_API_KEYS='<managed-key-pool>' \
  --from-literal=API_KEYS='<tenant-key-material>' \
  --from-literal=ADMIN_API_KEYS='<operator-key-material>'
```

Add optional provider, GitHub App, webhook, and BYOK values only when those
features are enabled. Never commit the command history or a populated Secret
manifest containing real credentials.

## Build and release

Build and publish the service and sandbox images using the existing CI workflow.
Use the ordered release script with the service/sandbox images tagged by the
same immutable Git commit SHA:

```bash
# Run from the repository root on a machine with kubectl and a selected context.
k8s/deploy.sh "$GIT_COMMIT_SHA"
```

Set `IMAGE_REPOSITORY` when using a registry other than the default. The script
works on a temporary copy, substitutes both image tags and the ConfigMap's
`SANDBOX_IMAGE`, applies namespace/config/storage/Postgres first, waits for the
Postgres StatefulSet, deletes any old immutable migration Job, applies the new
Job, waits for completion, and only then applies the API/worker Kustomization.
A failed migration blocks the application rollout.

The root Kustomization intentionally excludes `migration-job.yaml`; applying
`kubectl apply -k k8s/` directly is therefore only a resource render/apply
operation and does not perform migrations. Use `k8s/deploy.sh` for an ordered
release.

## Public webhook and health checks

Configure `ingress.yaml` separately after replacing `janus.example.com` and
creating `janus-tls`. The Ingress routes `/` to the API, including the GitHub
webhook endpoint. Confirm that the public endpoint reaches the API and that
`GET /healthz` reports database reachability before registering it in the GitHub
App settings.

The live GitHub App flow still requires a real App registration, installation,
public endpoint, and test repository. The repository's `eval_github_app.py`
module remains mocked/unit-level coverage and does not replace that operational
validation.

## Validation

Render manifests before every release. The sandbox has no target Kubernetes
API server, so offline rendering is the validation currently available here;
run server-side validation and inspect PVC support in the target cluster:

```bash
kubectl kustomize k8s/ > /tmp/janus-rendered.yaml
kubectl apply --dry-run=server -k k8s/
kubectl -n janus get deploy,statefulset,job,pods,svc,pvc
```

The Kubernetes path does not change Janus's application-level merge authority:
only the deterministic gate can authorize a merge, and admin visibility still
requires explicit `ADMIN_API_KEYS` configuration.
