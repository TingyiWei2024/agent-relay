# Agent Relay (SQLite starter)

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. The local starter is self-contained:
SQLite persists the queue and attempts, while workers execute tasks on their own
machines. The included worker deterministically returns `input.upper()`.

## Run it

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

`database.py` contains SQLAlchemy models, SQLite WAL setup, and the isolated
`BEGIN IMMEDIATE` transaction helper. `storage.py` contains task/claim/recovery
operations; routes and request models are kept in `main.py` and `schemas.py`.
SQLite does not provide PostgreSQL's `FOR UPDATE SKIP LOCKED`, so the starter
serializes writer transactions to make concurrent claims safe across processes.
Students can port this storage seam to PostgreSQL later without changing the
HTTP protocol or lifecycle in `SPEC.md`.

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

Stage B adds only a single-container SQLite runtime. Kubernetes, Compose,
CI, external brokers, an LLM, and a PostgreSQL implementation remain outside
this stage.
