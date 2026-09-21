# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. Docker Compose runs the API with
PostgreSQL; the self-contained local and single-container modes still support
SQLite. Workers execute tasks on their own machines. The included worker
deterministically returns `input.upper()`.

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
PostgreSQL Compose. Kubernetes, CI/CD, external brokers, and an LLM remain
outside this project stage.
