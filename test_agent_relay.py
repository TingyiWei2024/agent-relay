"""Protocol tests for the SQLite starter.

These tests intentionally exercise storage calls from multiple threads: that
is the closest local equivalent to several worker processes racing to claim an
inbox.  The production guarantee comes from SQLite's BEGIN IMMEDIATE boundary,
not from a Python lock.
"""

from __future__ import annotations

import os
from tempfile import TemporaryDirectory

# Configure a unique, real SQLite file before importing the application, which
# creates its engine and schema at import time. Never reset a caller's database.
_test_database = TemporaryDirectory(prefix="agent-relay-test-")
os.environ["RELAY_DATABASE_URL"] = f"sqlite:///{_test_database.name}/relay.db"

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import main
from database import Attempt, Base, Task, as_db_time, db_session, engine, utcnow
from storage import claim_one


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    yield
    engine.dispose()
    _test_database.cleanup()


@pytest.fixture(autouse=True)
def empty_database(isolated_database):
    # Reset only the temporary SQLite database configured above.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def register(client: TestClient, name: str) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201
    data = response.json()
    return data, {"Authorization": f"Bearer {data['token']}"}


def test_two_agents_send_claim_complete_and_sender_retrieves_result():
    """SPEC acceptance scenario 1, through the real API and SQLite storage."""
    task_input = "hello relay from agent A"
    task_output = "HELLO RELAY FROM AGENT A"
    with TestClient(main.app) as client:
        sender, sender_headers = register(client, "Agent A")
        recipient, recipient_headers = register(client, "Agent B")
        sender_id = sender["agent_id"]
        recipient_id = recipient["agent_id"]
        assert sender_id != recipient_id

        sent = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient_id, "input": task_input},
        )
        assert sent.status_code == 201
        task_id = sent.json()["task_id"]
        assert sent.json()["status"] == "queued"
        task_path = f"/api/v1/tasks/{task_id}"

        queued = client.get(task_path, headers=sender_headers)
        assert queued.status_code == 200
        queued_task = queued.json()
        assert queued_task["task_id"] == task_id
        assert queued_task["from"] == sender_id
        assert queued_task["to"] == recipient_id
        assert queued_task["input"] == task_input
        assert queued_task["status"] == "queued"
        assert queued_task["output"] is None
        assert queued_task["error"] is None
        assert queued_task["finished_at"] is None

        claimed = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "acceptance-worker-b", "wait_seconds": 0},
        )
        assert claimed.status_code == 200
        claim = claimed.json()
        claim_token = claim.pop("claim_token")
        assert bool(claim_token)
        assert claim["task_id"] == task_id
        assert claim["from"] == sender_id
        assert claim["input"] == task_input
        assert claim["attempt"] == 1
        assert claim["lease_expires_at"]

        processing = client.get(task_path, headers=sender_headers)
        assert processing.status_code == 200
        assert processing.json()["status"] == "processing"
        assert processing.json()["attempt_count"] == 1

        completed = client.post(
            f"{task_path}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_token, "output": task_output},
        )
        assert completed.status_code == 200
        assert completed.json() == {"task_id": task_id, "status": "completed"}

        retrieved = client.get(task_path, headers=sender_headers)
        assert retrieved.status_code == 200
        result = retrieved.json()
        assert result["task_id"] == task_id
        assert result["from"] == sender_id
        assert result["to"] == recipient_id
        assert result["input"] == task_input
        assert result["status"] == "completed"
        assert result["output"] == task_output
        assert result["error"] is None
        assert result["attempt_count"] == 1
        assert result["finished_at"] is not None


def test_protocol_idempotency_terminal_retry_and_auth_boundary():
    with TestClient(main.app) as client:
        sender, sender_headers = register(client, "sender")
        recipient, recipient_headers = register(client, "uppercase")
        sent = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "hello relay"},
        )
        assert sent.status_code == 201
        duplicate = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "hello relay"},
        )
        assert duplicate.status_code == 201
        assert duplicate.json() == sent.json()
        conflict = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "different"},
        )
        assert conflict.status_code == 409

        task_id = sent.json()["task_id"]
        claim = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "worker-a", "wait_seconds": 0},
        )
        assert claim.status_code == 200
        claim_data = claim.json()
        assert "claim_token" in claim_data
        complete = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_data["claim_token"], "output": "HELLO RELAY"},
        )
        assert complete.status_code == 200
        retry = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_data["claim_token"], "output": "HELLO RELAY"},
        )
        assert retry.status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}", headers=recipient_headers).status_code == 200
        forbidden = client.get(f"/api/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {sender['token']}"})
        assert forbidden.status_code == 200  # sender is an authorized participant
        no_credentials = client.get("/api/v1/agents")
        assert no_credentials.status_code == 401
        attempts = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=sender_headers).json()
        assert attempts["items"][0]["outcome"] == "completed"
        assert "claim_token" not in attempts["items"][0]


def test_sqlite_atomic_claims_distribute_without_overlap():
    with TestClient(main.app) as client:
        _sender, sender_headers = register(client, "sender")
        recipient, _recipient_headers = register(client, "recipient")
        for index in range(16):
            response = client.post(
                "/api/v1/tasks",
                headers=sender_headers,
                json={"to": recipient["agent_id"], "input": f"task-{index}"},
            )
            assert response.status_code == 201
        with ThreadPoolExecutor(max_workers=16) as pool:
            claims = list(pool.map(lambda index: claim_one(recipient["agent_id"], f"worker-{index}"), range(16)))
        claims = [claim for claim in claims if claim is not None]
        assert len(claims) == 16
        assert len({claim["task_id"] for claim in claims}) == 16
        with db_session() as db:
            processing = list(db.query(Task).filter(Task.status == "processing"))
            assert len(processing) == 16
            assert all(task.attempt_count == 1 for task in processing)


def test_expiry_requeues_and_old_token_is_stale_before_recovery():
    with TestClient(main.app) as client:
        _sender, sender_headers = register(client, "sender")
        recipient, recipient_headers = register(client, "recipient")
        task = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient["agent_id"], "input": "recover me"},
        ).json()
        task_id = task["task_id"]
        first = client.post(
            "/api/v1/tasks/claim", headers=recipient_headers, json={"worker_id": "dead", "wait_seconds": 0}
        ).json()
        with db_session() as db:
            attempt = db.query(Attempt).filter(Attempt.task_id == task_id).one()
            attempt.lease_expires_at = as_db_time(utcnow() - timedelta(seconds=1))
        stale = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": first["claim_token"], "output": "TOO LATE"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_claim"
        assert main.recover_expired() == 1
        second = client.post(
            "/api/v1/tasks/claim", headers=recipient_headers, json={"worker_id": "replacement", "wait_seconds": 0}
        )
        assert second.status_code == 200
        assert second.json()["attempt"] == 2
        assert second.json()["claim_token"] != first["claim_token"]


def test_dashboard_is_asset_and_invalid_input_is_documented_error():
    with TestClient(main.app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "sessionStorage" in page.text
        missing_name = client.post("/api/v1/agents", json={})
        assert missing_name.status_code == 400
        assert missing_name.json()["error"]["code"] == "invalid_input"
