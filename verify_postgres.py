"""Real PostgreSQL checks: docker compose exec -T app python - < verify_postgres.py.

Run in a fresh process, never in the SQLite pytest process. A unique schema is
created before importing the app and removed in finally; public data is untouched.
Uses only runtime dependencies and Python's unittest, so no test tools enter the
production image. Tokens remain in memory and are never printed.
"""

from __future__ import annotations

import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import timedelta
from threading import Barrier

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema


def verify() -> bool:
    url = make_url(os.environ["RELAY_DATABASE_URL"])
    if url.drivername != "postgresql+psycopg":
        raise SystemExit("This verification requires a real postgresql+psycopg URL.")
    schema = f"relay_verify_{uuid.uuid4().hex}"
    admin = create_engine(url)
    app_engine = None
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    try:
        # Exclude public from search_path: no reset or model query can touch it.
        isolated_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
        os.environ["RELAY_DATABASE_URL"] = isolated_url.render_as_string(hide_password=False)
        from fastapi.testclient import TestClient

        import main
        from database import (
            Agent, Attempt, Base, MAX_ATTEMPTS, Task, as_db_time, db_session,
            engine, recover_expired, utcnow, write_transaction,
        )
        from errors import RelayError
        from storage import (
            claim_one, commit_terminal, create_task, heartbeat, register_agent,
            task_for_participant,
        )

        app_engine = engine
        assert engine.dialect.name == "postgresql"

        class PostgreSQLChecks(unittest.TestCase):
            def setUp(self):
                Base.metadata.drop_all(engine)
                Base.metadata.create_all(engine)

            def pair(self):
                return register_agent("Agent A", None), register_agent("Agent B", None)

            def work(self):
                sender, recipient = self.pair()
                task = create_task(sender["agent_id"], recipient["agent_id"], "work", None)
                claim = claim_one(recipient["agent_id"], "postgres-worker")
                self.assertEqual(claim["task_id"], task["task_id"])
                return sender, recipient, claim

            def expire(self, task_id):
                with write_transaction() as db:
                    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
                    attempt = db.scalar(select(Attempt).where(
                        Attempt.task_id == task_id, Attempt.attempt_number == task.attempt_count,
                    ))
                    attempt.lease_expires_at = as_db_time(utcnow() - timedelta(seconds=1))

            def test_two_agents_api_and_direct_postgres_storage(self):
                task_input = "hello relay from agent A"
                task_output = "HELLO RELAY FROM AGENT A"
                with TestClient(main.app) as client:
                    self.assertEqual(client.get("/health").json(), {"status": "ok"})
                    self.assertEqual(client.get("/ready").json(), {"status": "ready"})
                    self.assertEqual(client.get("/").status_code, 200)

                    def register(name):
                        response = client.post("/api/v1/agents", json={"name": name})
                        self.assertEqual(response.status_code, 201)
                        data = response.json()
                        return data["agent_id"], {"Authorization": f"Bearer {data['token']}"}

                    sender, sender_auth = register("Agent A")
                    recipient, recipient_auth = register("Agent B")
                    self.assertNotEqual(sender, recipient)
                    sent = client.post("/api/v1/tasks", headers=sender_auth,
                                       json={"to": recipient, "input": task_input})
                    self.assertEqual(sent.status_code, 201)
                    self.assertEqual(sent.json()["status"], "queued")
                    task_id = sent.json()["task_id"]
                    path = f"/api/v1/tasks/{task_id}"
                    queued = client.get(path, headers=sender_auth).json()
                    self.assertEqual(queued["status"], "queued")
                    self.assertIsNone(queued["output"])
                    self.assertIsNone(queued["error"])
                    self.assertIsNone(queued["finished_at"])
                    response = client.post("/api/v1/tasks/claim", headers=recipient_auth,
                                           json={"worker_id": "postgres-worker-b", "wait_seconds": 0})
                    self.assertEqual(response.status_code, 200)
                    claim = response.json()
                    self.assertEqual((claim["task_id"], claim["from"], claim["input"], claim["attempt"]),
                                     (task_id, sender, task_input, 1))
                    self.assertTrue(claim["lease_expires_at"])
                    processing = client.get(path, headers=sender_auth).json()
                    self.assertEqual(processing["status"], "processing")
                    completed = client.post(path + "/complete", headers=recipient_auth,
                                            json={"claim_token": claim["claim_token"], "output": task_output})
                    self.assertEqual(completed.status_code, 200)
                    self.assertEqual(completed.json(), {"task_id": task_id, "status": "completed"})
                    result = client.get(path, headers=sender_auth).json()
                    for state in (queued, processing, result):
                        self.assertEqual((state["task_id"], state["from"], state["to"], state["input"]),
                                         (task_id, sender, recipient, task_input))
                    self.assertEqual((result["status"], result["output"], result["attempt_count"]),
                                     ("completed", task_output, 1))
                    self.assertIsNone(result["error"])
                    self.assertTrue(result["created_at"])
                    self.assertTrue(result["finished_at"])
                    history = client.get(path + "/attempts", headers=sender_auth).json()["items"]
                    self.assertEqual(len(history), 1)
                    self.assertEqual(history[0]["outcome"], "completed")
                    self.assertNotIn("claim_token", history[0])
                    self.assertEqual(client.get(path).status_code, 401)
                    _, outsider_auth = register("Outsider")
                    self.assertEqual(client.get(path, headers=outsider_auth).status_code, 404)
                    with db_session() as db:
                        identity = db.execute(text("SELECT current_database(), current_schema(), version()")).one()
                        self.assertEqual(identity[1], schema)
                        self.assertIn("PostgreSQL", identity[2])
                        stored = db.get(Task, task_id)
                        self.assertEqual((stored.sender_id, stored.recipient_id, stored.output),
                                         (sender, recipient, task_output))
                        self.assertEqual(db.scalar(select(func.count()).select_from(Agent)), 3)
                        self.assertEqual(db.scalar(select(func.count()).select_from(Attempt)), 1)
                    print(json.dumps({"database": identity[0], "schema": schema, "version": identity[2],
                                      "lifecycle": ["queued", "processing", "completed"], "task": result,
                                      "attempts": history}), flush=True)

            def test_concurrent_claims_and_skip_locked_oldest(self):
                sender, recipient = self.pair()
                task_ids = [create_task(sender["agent_id"], recipient["agent_id"], str(i), None)["task_id"]
                            for i in range(16)]
                with ThreadPoolExecutor(max_workers=16) as pool:
                    with write_transaction() as db:
                        db.scalar(select(Task).where(Task.id == task_ids[0]).with_for_update())
                        # A locked oldest task must not block work on the next task.
                        next_claim = pool.submit(claim_one, recipient["agent_id"], "skip-locked").result(timeout=5)
                        self.assertEqual(next_claim["task_id"], task_ids[1])
                    barrier = Barrier(16)

                    def claim(index):
                        barrier.wait(timeout=5)
                        return claim_one(recipient["agent_id"], f"worker-{index}")

                    claims = [value for value in pool.map(claim, range(16)) if value is not None]
                self.assertEqual(len(claims), 15)
                self.assertEqual({next_claim["task_id"], *(c["task_id"] for c in claims)}, set(task_ids))
                with db_session() as db:
                    self.assertEqual(db.scalar(select(func.count()).select_from(Attempt)), 16)
                    self.assertTrue(all(t.attempt_count == 1 for t in db.scalars(select(Task))))

            def test_concurrent_idempotent_and_opposing_sends(self):
                sender, recipient = self.pair()
                barrier = Barrier(12)

                def send(_):
                    barrier.wait(timeout=5)
                    return create_task(sender["agent_id"], recipient["agent_id"], "same", "same-key")

                with ThreadPoolExecutor(max_workers=12) as pool:
                    tasks = list(pool.map(send, range(12)))
                    self.assertEqual(len({t["task_id"] for t in tasks}), 1)
                    # Opposing foreign-key checks must not deadlock sender locks.
                    barrier = Barrier(2)

                    def opposite(reverse):
                        a, b = (recipient, sender) if reverse else (sender, recipient)
                        barrier.wait(timeout=5)
                        return create_task(a["agent_id"], b["agent_id"], "opposite", "opposite-key")

                    self.assertEqual(len(list(pool.map(opposite, [False, True]))), 2)
                with self.assertRaises(RelayError) as caught:
                    create_task(sender["agent_id"], recipient["agent_id"], "different", "same-key")
                self.assertEqual(caught.exception.code, "idempotency_conflict")
                with db_session() as db:
                    self.assertEqual(db.scalar(select(func.count()).select_from(Task)), 3)

            def test_concurrent_terminal_retries_and_conflicts(self):
                sender, recipient, claim = self.work()
                barrier = Barrier(8)

                def complete(_):
                    barrier.wait(timeout=5)
                    return commit_terminal(claim["task_id"], recipient["agent_id"], claim["claim_token"],
                                           action="complete", value="accepted")

                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(complete, range(8)))
                self.assertTrue(all(r["status"] == "completed" for r in results))
                self.expire(claim["task_id"])
                retry = commit_terminal(claim["task_id"], recipient["agent_id"], claim["claim_token"],
                                        action="complete", value="accepted")
                self.assertEqual(retry["status"], "completed")
                with self.assertRaises(RelayError) as caught:
                    commit_terminal(claim["task_id"], recipient["agent_id"], claim["claim_token"],
                                    action="fail", value="conflict")
                self.assertEqual(caught.exception.code, "conflicting_terminal")
                self.assertEqual(task_for_participant(claim["task_id"], sender["agent_id"]).output, "accepted")

            def test_expiry_recovery_and_attempt_limit(self):
                sender, recipient, first = self.work()
                claim = first
                for number in range(1, MAX_ATTEMPTS + 1):
                    self.expire(claim["task_id"])
                    for action in (lambda: heartbeat(claim["task_id"], recipient["agent_id"], claim["claim_token"]),
                                   lambda: commit_terminal(claim["task_id"], recipient["agent_id"], claim["claim_token"],
                                                           action="complete", value="too late")):
                        with self.assertRaises(RelayError) as caught:
                            action()
                        self.assertEqual(caught.exception.code, "stale_claim")
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        self.assertEqual(sum(pool.map(lambda _: recover_expired(), range(4))), 1)
                    if number < MAX_ATTEMPTS:
                        claim = claim_one(recipient["agent_id"], "replacement")
                        self.assertEqual(claim["attempt"], number + 1)
                        self.assertTrue(claim["claim_token"] != first["claim_token"])
                self.assertIsNone(claim_one(recipient["agent_id"], "exhausted"))
                task = task_for_participant(first["task_id"], sender["agent_id"])
                self.assertEqual((task.status, task.error, task.attempt_count), ("failed", "attempts_exhausted", MAX_ATTEMPTS))

            def test_heartbeat_and_recovery_coordinate_on_task_lock(self):
                _, recipient, claim = self.work()
                self.expire(claim["task_id"])
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with write_transaction() as db:
                        db.scalar(select(Task).where(Task.id == claim["task_id"]).with_for_update())
                        self.assertEqual(pool.submit(recover_expired).result(timeout=5), 0)
                        attempt = db.scalar(select(Attempt).where(Attempt.task_id == claim["task_id"]))
                        attempt.lease_expires_at = as_db_time(utcnow() + timedelta(seconds=60))
                    heartbeat(claim["task_id"], recipient["agent_id"], claim["claim_token"])
                    self.assertEqual(recover_expired(), 0)
                    self.assertIsNone(claim_one(recipient["agent_id"], "other-worker"))
                    with write_transaction() as db:
                        db.scalar(select(Task).where(Task.id == claim["task_id"]).with_for_update())
                        waiting = pool.submit(heartbeat, claim["task_id"], recipient["agent_id"], claim["claim_token"])
                        with self.assertRaises(TimeoutError):
                            waiting.result(timeout=0.2)
                        attempt = db.scalar(select(Attempt).where(Attempt.task_id == claim["task_id"]))
                        attempt.lease_expires_at = as_db_time(utcnow() - timedelta(seconds=1))
                    with self.assertRaises(RelayError) as caught:
                        waiting.result(timeout=5)
                    self.assertEqual(caught.exception.code, "stale_claim")
                self.assertEqual(recover_expired(), 1)

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(PostgreSQLChecks)
        return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()
    finally:
        if app_engine is not None:
            app_engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
        print(f"Removed isolated PostgreSQL schema {schema}", flush=True)


if __name__ == "__main__":
    raise SystemExit(0 if verify() else 1)
