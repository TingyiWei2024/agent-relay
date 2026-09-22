# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. Docker Compose runs the API with
PostgreSQL; the self-contained local and single-container modes still support
SQLite. Workers execute tasks on their own machines. The included worker
deterministically returns `input.upper()`.

## Test and deploy locally with act (Stage E)

`.github/workflows/ci.yml` uses GitHub Actions syntax but is intentionally run
locally through `act`. A GitHub-hosted runner cannot reach this local kind
cluster; the workflow refuses execution outside act. No application registry,
cloud account or GitHub token is needed. Check the existing tools first:

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" # macOS Docker Desktop, if needed
docker version
kind version
kubectl version --client
act --version
uv --version
```

Do not run concurrent deployments to the same cluster. Bootstrap a dedicated
cluster once using the Stage D commands below (including its initial app image,
manifests and Ready checks). Keep that cluster and its PostgreSQL PVC across
workflow runs. The examples below use `agent-relay`; substitute your dedicated
cluster name consistently. Existing Compose/local databases are not used by CI.

Build the local Linux runner, with a separate build context from the app's
allowlisted `.dockerignore`:

```bash
docker build -t agent-relay-act:local ci/act-runner
bash ci/run-act.sh agent-relay
```

The runner supplies Python 3.11.16, uv 0.12.9, Node 22.16.0, Docker CLI 28.0.4,
kind 0.31.0 and kubectl 1.32.2. Tool downloads are versioned; kind and kubectl
downloads are checksum-verified. It supports native Linux ARM64/AMD64 tools;
Darwin host binaries are not mounted into Linux. These tools stay outside the
production app image. First builds need access to upstream image/package/tool
registries, and later runs still need any uncached Python packages.

`ci/run-act.sh` requires an existing cluster and runner image. It makes a private
temporary internal kubeconfig with `kind get kubeconfig --name "$CLUSTER"
--internal`, mounts that file read-only, and joins Docker's `kind` network so
the Kubernetes API hostname is reachable with certificate verification intact.
It mounts the Docker socket so Linux Docker and kind can load images into the
same local engine. It does not change the host kubeconfig, context or software.
The wrapper's expanded invocation is:

```bash
XDG_CACHE_HOME=/tmp/agent-relay-act-cache act workflow_dispatch -W .github/workflows/ci.yml -j local-kind \
  -P ubuntu-latest=agent-relay-act:local --pull=false \
  --container-architecture "linux/$ARCH" --container-daemon-socket /var/run/docker.sock \
  --network kind --container-options "--volume $RELAY_ACT_DIR/kubeconfig:/run/relay/kubeconfig:ro" \
  --env KUBECONFIG=/run/relay/kubeconfig --env RELAY_KIND_CLUSTER="$CLUSTER" \
  --env RELAY_CI_RUN="$RUN_VALUE" --action-cache-path /tmp/agent-relay-act-actions \
  --no-cache-server --rm
```

The wrapper sets `ARCH` from the Docker server and creates a fresh UUID
`RUN_VALUE` every time, including repeated runs of the same commit. act copies
the candidate checkout into its runner. Start from a clean committed checkout
for a deployable version; `GITHUB_SHA` identifies that commit. No host `.venv`
is used: runner environments and caches live under `/tmp`.

The single `local-kind` job runs sequential steps:

1. Validate local execution, target cluster and unique candidate tag.
2. Run `UV_FROZEN=1 uv run pytest -q` against the candidate (five existing SQLite tests).
3. Start a disposable PostgreSQL container with tmpfs storage, then run
   `RELAY_DATABASE_URL="$CI_POSTGRES_URL" UV_FROZEN=1 uv run python verify_postgres.py`
   against candidate code (six checks including the accepted two-agent flow).
   `CI_POSTGRES_URL` points to that CI-only container. The verifier additionally
   uses its existing UUID schema isolation and cleanup. It never connects to or
   resets the live Kubernetes database. The two suites use separate processes.
4. Build `agent-relay:ci-<12-character-commit>-<32-character-run-UUID>`, recording
   the Docker image ID and BuildKit config digest. Existing tags are rejected
   rather than overwritten.
5. Run `kind load docker-image "$IMAGE" --name "$RELAY_KIND_CLUSTER"` and confirm
   each kind node's CRI image ID matches the built config digest. Docker's
   containerd image store may report an OCI index digest as its image ID, so
   comparing that index directly with CRI's config digest would be incorrect.
6. Run `kubectl --context "kind-$RELAY_KIND_CLUSTER" -n agent-relay set image
   deployment/agent-relay app="$IMAGE" wait-for-postgres="$IMAGE"`, then
   `kubectl --context "kind-$RELAY_KIND_CLUSTER" -n agent-relay rollout status
   deployment/agent-relay --timeout=180s`. Verify the current observed generation,
   one updated/Ready/available replica, both exact image references and `Never`
   pull policies. The log records the Pod UID and runtime image IDs.
7. Always remove only the disposable CI PostgreSQL container, if it was created.

Every build/load/deploy step requires success of all preceding required tests.
Test failure returns nonzero from act before building/loading/deploying the
candidate. Cleanup never deletes the existing Deployment, StatefulSet, PVC or
cluster. Rollout failure also returns nonzero; the workflow does not hide it
or automatically delete workloads. The accepted one-replica `Recreate`
strategy means a successful update has brief downtime. Do not reapply the
generic `agent-relay:kind` app manifest between successful workflow runs.

### Verify deployment and the failure gate

After a successful run, use the Stage D port-forward and acceptance commands
below. Restart port forwarding when the app Pod changes. Verify health/readiness,
the visible heading, a real two-agent exchange, direct PostgreSQL records and
previously retained results/PVC. The intentional later Homework version changes
only the visible heading to `Agent Relay v2`, after independent initial workflow
verification, and runs the same workflow against the same surviving cluster.

To prove a required test prevents deployment, use an isolated local clone while
the successful deployment is running. Run these commands from the real checkout
with the Stage D temporary `KUBECONFIG` still set:

```bash
export RELAY_FAILURE_DIR="$(mktemp -d /tmp/agent-relay-failure.XXXXXX)"
kubectl --context kind-agent-relay -n agent-relay get deployment agent-relay -o json > "$RELAY_FAILURE_DIR/deployment-before.json"
kubectl --context kind-agent-relay -n agent-relay get pods -l app=agent-relay -o json > "$RELAY_FAILURE_DIR/pods-before.json"
git clone --no-hardlinks . "$RELAY_FAILURE_DIR/candidate"
git -C "$RELAY_FAILURE_DIR/candidate" remote set-url origin "$(git remote get-url origin)"
printf 'def test_required_failure_gate():\n    assert False, "intentional isolated CI gate check"\n' > "$RELAY_FAILURE_DIR/candidate/test_required_failure_gate.py"
(cd "$RELAY_FAILURE_DIR/candidate" && bash ci/run-act.sh agent-relay) > "$RELAY_FAILURE_DIR/act-failure.log" 2>&1
# The previous command MUST exit nonzero. Inspect the log: pytest fails and
# Build/Load/Update steps never execute; only CI database cleanup may run.
kubectl --context kind-agent-relay -n agent-relay get deployment agent-relay -o json > "$RELAY_FAILURE_DIR/deployment-after.json"
kubectl --context kind-agent-relay -n agent-relay get pods -l app=agent-relay -o json > "$RELAY_FAILURE_DIR/pods-after.json"
cmp "$RELAY_FAILURE_DIR/deployment-before.json" "$RELAY_FAILURE_DIR/deployment-after.json"
cmp "$RELAY_FAILURE_DIR/pods-before.json" "$RELAY_FAILURE_DIR/pods-after.json"
```

Confirm the same Deployment UID/generation/template/images and Ready Pod UID,
and retrieve the same authenticated task result again. Only the disposable clone
contains the failing test; do not commit it or edit the real tests. The workflow
log prints each unique tag so the failed candidate's absence can be checked with
`docker image inspect <failed-candidate-tag>`. Keep evidence outside the repository.
Delete the temporary clone after review. Stop port forwarding and use the Stage D
cluster cleanup only when its homework database is no longer needed; application
updates themselves retain the cluster/PVC. CI PostgreSQL containers are removed
automatically, and the wrapper removes its temporary kubeconfig. Built app images
and the reusable `agent-relay-act:local` runner remain local.

References: [act runner mapping and local images](https://nektosact.com/usage/runners.html),
[act usage](https://nektosact.com/usage/index.html), and
[kind image loading](https://kind.sigs.k8s.io/docs/user/quick-start/#loading-an-image-into-your-cluster).

## Run with PostgreSQL and local kind (Stage D)

Start Docker Desktop/the Docker engine and make host port 8000 available.
From the repository root, check the installed tools before continuing:

```bash
docker --version
docker info
kind version
kubectl version --client
kind get clusters
```

If a tool is missing, install it separately before proceeding. On macOS, the
Docker Desktop CLI can be used in this terminal with
`export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"`.
Use the dedicated cluster name `agent-relay`; if that name already belongs to
another project, choose a different name consistently in the commands below.
These commands use a separate temporary kubeconfig and do not change the
default kubeconfig or another cluster's context:

```bash
export RELAY_KIND_DIR="$(mktemp -d /tmp/agent-relay-kind.XXXXXX)"
export KUBECONFIG="$RELAY_KIND_DIR/kubeconfig"
kind create cluster --name agent-relay \
  --image kindest/node:v1.32.11@sha256:5fc52d52a7b9574015299724bd68f183702956aa4a2116ae75a63cb574b35af8 \
  --kubeconfig "$KUBECONFIG" --wait 120s
kubectl --context kind-agent-relay wait --for=condition=Ready nodes --all --timeout=120s
docker build -t agent-relay:kind .
kind load docker-image agent-relay:kind --name agent-relay
kubectl --context kind-agent-relay apply -f k8s/
kubectl --context kind-agent-relay -n agent-relay rollout status statefulset/postgres --timeout=180s
kubectl --context kind-agent-relay -n agent-relay rollout status deployment/agent-relay --timeout=180s
kubectl --context kind-agent-relay -n agent-relay wait --for=condition=Ready pod --all --timeout=120s
kubectl --context kind-agent-relay -n agent-relay get deployments,statefulsets,pods,services,pvc
kubectl --context kind-agent-relay -n agent-relay get endpoints
kubectl --context kind-agent-relay get storageclass,pv
```

The pinned node image is published in the [kind v0.31 release](https://github.com/kubernetes-sigs/kind/releases/tag/v0.31.0).
Kubernetes 1.32 keeps this local exercise compatible with the installed
kubectl 1.32 client; [kubectl must stay within one minor version of the API server](https://kubernetes.io/releases/version-skew-policy/#kubectl).
This is a local homework configuration, not a production version recommendation.
The application image uses the accepted Dockerfile. Both the app and its init
container specify `imagePullPolicy: Never`, so forgetting `kind load` produces
an explicit image error instead of a remote registry pull. PostgreSQL and the
kind node image may require registry access on their first use. After rebuilding
the same app tag, load it again and restart the Deployment to use the new image.

All application resources live in namespace `agent-relay`:

| Resource | Name and purpose |
| --- | --- |
| Deployment | `agent-relay`, one replica, `Recreate` updates |
| Service | `agent-relay`, internal ClusterIP, port 8000 |
| StatefulSet | `postgres`, one replica (`postgres-0`), PostgreSQL 17.11 |
| Service | `postgres`, headless stable DNS, port 5432 |
| ConfigMap / Secret | `postgres-config` / `postgres-local`, local database settings |
| PVC | `postgres-data-postgres-0`, 1 GiB, ReadWriteOnce, `standard` StorageClass |

The app connects through SQLAlchemy/psycopg to
`postgresql+psycopg://relay:relay-local-only@postgres:5432/agent_relay`.
`postgres` resolves the PostgreSQL Service within the namespace; `localhost`
would incorrectly address the app Pod. The checked-in Secret contains public
homework defaults, not personal credentials. It is not a production secret
management system. No PostgreSQL host port or public load balancer is created.

An init container uses the app's existing dependencies to wait for a successful
database connection before app import initializes tables. PostgreSQL readiness
runs `pg_isready` every two seconds. App readiness calls the existing `/ready`
every two seconds with a three-second timeout; it queries the actual database
tables. The independent `/health` liveness probe checks the API process. Check
actual Ready conditions, rollout success, Service endpoints and a Bound PVC;
successful `apply` alone does not prove readiness. One app replica and Recreate
updates keep this starter simple, with brief downtime during replacement.

### Port forwarding and real acceptance

Keep the following process running in the cluster terminal:

```bash
kubectl --context kind-agent-relay -n agent-relay port-forward --address 127.0.0.1 service/agent-relay 8000:8000
```

Open <http://127.0.0.1:8000/>. In another terminal, verify:

```bash
curl --fail http://127.0.0.1:8000/health  # {"status":"ok"}
curl --fail http://127.0.0.1:8000/ready   # {"status":"ready"}
curl --fail http://127.0.0.1:8000/        # dashboard HTML
```

Follow the accepted `SPEC.md` flow through this real HTTP address: register
distinct A and B, send nonempty input from A to B (`queued`), claim as B
(`processing`), complete using B's bearer token and active claim token, then
retrieve as A and confirm `completed` and the exact output. Retain A's token
and task ID outside source control. Enter A's token in the dashboard, click
**Use token**, and verify that same task, output, and completed delivery history.
The host browser/worker reaches the app Service through port forwarding; the
app reaches the PostgreSQL Pod through its Service and stores records on the PVC.

For the remaining commands, use a terminal with the same `KUBECONFIG` value
printed by `echo "$KUBECONFIG"` in the cluster terminal. Before registration,
a **fresh cluster and PVC** must show zero agents, tasks and attempts:

```bash
kubectl --context kind-agent-relay -n agent-relay exec postgres-0 -- psql -U relay -d agent_relay -c 'SELECT (SELECT count(*) FROM agents) AS agents, (SELECT count(*) FROM tasks) AS tasks, (SELECT count(*) FROM attempts) AS attempts;'
```

After acceptance, directly confirm the same records in Kubernetes PostgreSQL
without printing credential hashes:

```bash
kubectl --context kind-agent-relay -n agent-relay exec postgres-0 -- psql -U relay -d agent_relay -c 'SELECT current_database(), version();'
kubectl --context kind-agent-relay -n agent-relay exec postgres-0 -- psql -U relay -d agent_relay -c 'SELECT id, name, created_at FROM agents;'
kubectl --context kind-agent-relay -n agent-relay exec postgres-0 -- psql -U relay -d agent_relay -c 'SELECT id, sender_id, recipient_id, input, status, output, attempt_count, finished_at FROM tasks;'
kubectl --context kind-agent-relay -n agent-relay exec postgres-0 -- psql -U relay -d agent_relay -c 'SELECT task_id, attempt_number, worker_id, outcome, claimed_at, lease_expires_at, finished_at FROM attempts;'
kubectl --context kind-agent-relay -n agent-relay exec deployment/agent-relay -- python -c 'from database import engine; print(engine.url.render_as_string(hide_password=True))'
```

### Verify Pod replacement and regression

Record Pod UIDs and the PVC UID/PV binding before replacement:

```bash
kubectl --context kind-agent-relay -n agent-relay get pods -o custom-columns=NAME:.metadata.name,UID:.metadata.uid
kubectl --context kind-agent-relay -n agent-relay get pvc postgres-data-postgres-0 -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,PV:.spec.volumeName,STATUS:.status.phase
kubectl --context kind-agent-relay -n agent-relay rollout restart deployment/agent-relay
kubectl --context kind-agent-relay -n agent-relay rollout status deployment/agent-relay --timeout=120s
```

Restart port forwarding after the app Pod is replaced. Using the saved A token,
retrieve the same task ID over HTTP and refresh the dashboard; both must show
the identical completed result. Record the changed app Pod UID. Then replace
only the PostgreSQL Pod, retaining its claim and volume:

```bash
kubectl --context kind-agent-relay -n agent-relay delete pod postgres-0
kubectl --context kind-agent-relay -n agent-relay rollout status statefulset/postgres --timeout=180s
kubectl --context kind-agent-relay -n agent-relay wait --for=condition=Ready pod --all --timeout=120s
kubectl --context kind-agent-relay -n agent-relay get pods -o custom-columns=NAME:.metadata.name,UID:.metadata.uid
kubectl --context kind-agent-relay -n agent-relay get pvc postgres-data-postgres-0 -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,PV:.spec.volumeName,STATUS:.status.phase
```

Confirm a changed PostgreSQL Pod UID but the **same PVC UID and PV binding**.
Wait for `/ready` to return 200, retrieve the identical authenticated result,
refresh the dashboard, and repeat the direct SQL checks. Temporary app readiness
failures during database replacement are expected. Do not delete the PVC to
test persistence. kind's local-path `standard` provisioner stores the volume
inside the kind node: data survives Pod replacement, **not cluster deletion**.
This single-node setup provides neither database replication nor backups.

Run the unchanged SQLite regression suite and the accepted isolated PostgreSQL
verifier (the latter uses a separate temporary schema and removes it afterward):

```bash
UV_FROZEN=1 uv run pytest -q
kubectl --context kind-agent-relay -n agent-relay exec -i deployment/agent-relay -- python - < verify_postgres.py
```

Expect five SQLite tests and six PostgreSQL checks to pass. Confirm the public
acceptance records remain unchanged after the verifier. These tests complement
the real port-forwarded HTTP/browser acceptance; they do not replace it.

### Clean up this local cluster

Stop port forwarding with Ctrl-C. To stop workloads while retaining this
cluster's PostgreSQL PVC, delete only the workload and Service manifests:

```bash
kubectl --context kind-agent-relay delete -f k8s/03-app.yaml -f k8s/02-postgres.yaml
```

Reapply `k8s/` to reuse the retained PVC. When all homework data can be discarded,
delete **only** the dedicated cluster and its temporary kubeconfig:

```bash
kind delete cluster --name agent-relay --kubeconfig "$KUBECONFIG"
rm -- "$KUBECONFIG"
rmdir -- "$RELAY_KIND_DIR"
unset KUBECONFIG RELAY_KIND_DIR
```

Cluster deletion destroys its PostgreSQL data. Deleting namespace `agent-relay`
or its PVC also destroys the local volume; do not use `kubectl delete -f k8s/`
when intending to preserve data. Stages A/B/C remain available unchanged.
CI/CD, GitHub Actions, `act`, public cloud deployment and unrelated features
are outside Stage D.

## Run with PostgreSQL and Compose (Stage C)

Start Docker Desktop/the Docker engine. From the repository root, with host
port 8000 free:

```bash
docker --version
docker info
docker compose version
docker compose up --build
```

On macOS, if Docker Desktop is installed but its CLI is not on your PATH, use
`export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"` in this
terminal. This does not change host configuration.

Open <http://localhost:8000/>. Compose starts exactly two services: `app` and
`postgres`. The browser/worker calls host port 8000, which forwards to Uvicorn
on `0.0.0.0:8000`; the app uses SQLAlchemy and the existing locked
`psycopg[binary]` 3.3.5 driver to reach PostgreSQL 17.11. No dependency additions
are needed. The app's URL is:

```text
postgresql+psycopg://relay:relay-local-only@postgres:5432/agent_relay
```

These explicit credentials are public, local homework defaults, not production
secrets. The database hostname **must be `postgres`**: Compose service discovery
resolves that name on its network. Inside the app container, `localhost` refers
to the app itself. PostgreSQL port 5432 is not published to the host.

The database health check runs `pg_isready`; `app` waits for
`postgres` to become healthy via `depends_on: condition: service_healthy`.
SQLAlchemy `create_all` initializes missing tables on a fresh database during
app startup. `/ready` then queries all three real tables, and the image health
check calls `/ready`. There is no migration framework or SQLite data import.

The named volume `postgres-data` mounts at `/var/lib/postgresql/data`. Docker
prefixes its actual name with the Compose project (normally
`agent-relay_postgres-data`). PostgreSQL data survives app restarts,
replacement, and `docker compose down`; retain the same project name to reuse
the same volume. **`docker compose down -v` deletes that project's database
volume and all its data.**

In another terminal:

```bash
docker compose ps
curl --fail http://localhost:8000/health  # {"status":"ok"}
curl --fail http://localhost:8000/ready   # {"status":"ready"}
curl --fail http://localhost:8000/        # dashboard HTML
docker compose exec -T app python -c 'from database import engine; print(engine.url.render_as_string(hide_password=True))'
```

Register A and B using the example below, then follow the accepted flow in
`SPEC.md` through `http://localhost:8000/api/v1`: A sends (`queued`), B claims
(`processing`), B completes with B's bearer token and active claim token, and A
retrieves the exact output (`completed`). Enter A's token in the dashboard and
click **Use token** to display that task, result, and completed delivery history.
Keep bearer/claim tokens out of logs and source control.

Directly inspect PostgreSQL without exposing credential hashes:

```bash
docker compose exec -T postgres psql -U relay -d agent_relay -c 'SELECT current_database(), version();'
docker compose exec -T postgres psql -U relay -d agent_relay -c 'SELECT id, name, created_at FROM agents;'
docker compose exec -T postgres psql -U relay -d agent_relay -c 'SELECT id, sender_id, recipient_id, input, status, output, attempt_count, finished_at FROM tasks;'
docker compose exec -T postgres psql -U relay -d agent_relay -c 'SELECT task_id, attempt_number, worker_id, outcome, claimed_at, lease_expires_at, finished_at FROM attempts;'
```

Retain A's token and task ID, then verify the same authenticated result and
dashboard after each command (PostgreSQL stays running):

```bash
docker compose restart app
docker compose up -d --no-deps --force-recreate app
```

Wait for `/ready` to return 200 after each restart/replacement. The PostgreSQL
container ID and named volume should remain the same. Stop the stack with
Ctrl-C and/or `docker compose down`. No host SQLite file is used by Compose.

### Isolated real PostgreSQL integration verification

With the stack running, execute this from the repository root:

```bash
docker compose exec -T app python - < verify_postgres.py
UV_FROZEN=1 uv run pytest -q
```

The first command runs six checks using the real app and real Compose
PostgreSQL: the complete two-agent API lifecycle plus SQL evidence, concurrent
claims and locked-row skipping, concurrent idempotent/opposing sends, terminal
retries/conflicts, expired-lease recovery and attempt exhaustion, and heartbeat
coordination with recovery. It uses a fresh UUID-named schema configured before
app import, excludes `public` from its search path, and drops only its own
schema in `finally`. Normal Compose development records remain intact. The
script uses runtime dependencies and Python `unittest`; tests/dev tools are
not copied into the production image. Run it in its own process, because the
unchanged SQLite pytest module always configures a temporary SQLite database.

The PostgreSQL script verifies the API in-process against real PostgreSQL;
the published-port HTTP and browser checks above separately verify Compose
networking and dashboard rendering. For a disposable verification stack,
prefix **every** Compose command with `docker compose -p relay-stage-c-check`
in place of `docker compose`, then remove only that temporary project's data
with `docker compose -p relay-stage-c-check down -v` when finished.

## Run locally with SQLite

```bash
uv sync
uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. The default
database is `./agent-relay.db`; set `RELAY_DATABASE_URL` to use another SQLite
file. `GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so a wiped volume
reports not-ready instead of passing with zero tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains SQLAlchemy models, backend transaction setup, and lease
recovery. `storage.py` contains task operations; routes and request models stay
in `main.py` and `schemas.py`. PostgreSQL claims lock the oldest available task
using `FOR UPDATE SKIP LOCKED`. Heartbeat, terminal submission, and recovery
lock the task before reading its current attempt, and check lease time after
the lock. Task and attempt updates commit atomically. Sender row locks use
`FOR NO KEY UPDATE` for idempotent creation, allowing opposing foreign-key
checks without deadlocking. SQLite retains WAL and `BEGIN IMMEDIATE` writer
serialization; SQLAlchemy omits row-lock clauses for SQLite. No process-local
lock provides the delivery guarantee.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q
```

Tests always use a unique temporary SQLite file, overriding
`RELAY_DATABASE_URL` before importing the app. The existing reset fixture
only drops and recreates tables in that temporary database, which is removed
after the test session; your dev server's `./agent-relay.db` is untouched.
The focused acceptance test registers two agents, sends a task, claims and
completes it as the recipient, and retrieves the completed result as the sender.

## Run in Docker (Stage B)

Start the Docker engine, then build from the repository root:

```bash
docker --version
docker info
docker build -t agent-relay:local .
docker run --rm -p 8000:8000 --name agent-relay agent-relay:local
```

Stop any local development server using port 8000 first. Open
<http://localhost:8000/> for the same dashboard and use
`http://localhost:8000/api/v1` for the same API. The existing worker commands
above can run on the host against this published port.

The image runs Uvicorn directly, without reload, as an unprivileged user. It
listens on `0.0.0.0:8000` inside the container so traffic arriving on the
container's network interface can reach it. Binding only `127.0.0.1` inside
the container would restrict access to its own loopback interface.
`-p 8000:8000` publishes host port 8000 to container port 8000; `EXPOSE` alone
does not publish a port. To restrict publishing to the host's loopback
interface, use `-p 127.0.0.1:8000:8000` instead.

The Dockerfile uses digest-pinned Python 3.11.16 on Debian bookworm slim and
uv 0.12.9. A dependency stage runs `uv sync --frozen --no-dev` against the
checked-in `uv.lock`, without resolving new versions or downloading another
Python runtime. Only the resulting virtual environment and explicit runtime
source/assets enter the final image; uv, tests, and dependency caches stay
out. `httpx` is a runtime dependency because the included `worker.py` imports
it; moving it out of the dev group preserves its existing locked version and
all other locked versions. There is no Node build or separate frontend.
The `.dockerignore` allows only named build inputs, excluding local databases,
credentials, Git metadata, and virtual environments from the build context.

### SQLite lifetime

By default, `RELAY_DATABASE_URL=sqlite:////data/agent-relay.db` stores SQLite
and its WAL sidecars in the container's writable `/data` directory. With the
command above, `--rm` removes the container and its database after it stops.
For data that survives replacing the container, use a named volume:

```bash
docker run --rm -p 8000:8000 --name agent-relay \
  -v agent-relay-data:/data \
  agent-relay:local
```

`-v agent-relay-data:/data` creates or reuses a Docker-managed volume mounted
at `/data`. The database remains `/data/agent-relay.db` in the container; it
does not use the host checkout's `./agent-relay.db`. Reuse the same volume
to retain agents, tasks, results, and attempts. Keep participant credentials
on the client to access those records. Stop either run with Ctrl-C, or from
another terminal with `docker stop agent-relay`. A named volume survives
`--rm`; remove it with `docker volume rm agent-relay-data` only when its data
is no longer needed.

### Verify the container

With the container running, call its published host port:

```bash
curl --fail http://localhost:8000/health  # {"status":"ok"}
curl --fail http://localhost:8000/ready   # {"status":"ready"}
curl --fail http://localhost:8000/        # Agent Relay dashboard HTML
```

Use the registration example above with `http://localhost:8000`, then follow
the two-agent flow in `SPEC.md` against that same base URL: A sends a task to
B (`queued`), B claims it (`processing`), B completes with its bearer token
and active claim token, and A retrieves the exact output (`completed`).
Enter A's or B's token in the dashboard and click **Use token** to see the
task, result, and completed delivery history. The image's health check also
calls `/ready`; inspect it with `docker inspect agent-relay`.

Run the unchanged Stage A regression suite on the host:

```bash
UV_FROZEN=1 uv run pytest -q
UV_FROZEN=1 uv run pytest -q test_agent_relay.py::test_two_agents_send_claim_complete_and_sender_retrieves_result
```

`UV_FROZEN=1` keeps the committed lockfile intact during these `uv run`
commands. The tests use their own temporary SQLite file; they do not call
the container, so the live HTTP verification above is also required.

The Stage B single-container SQLite mode remains available alongside Stage C
PostgreSQL Compose and Stage D local Kubernetes. CI/CD, external brokers, and
an LLM remain outside this project stage.
