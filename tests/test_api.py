import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

import app as app_module


class FakeProcess:
    def __init__(self, output=b"", return_code=0):
        self.stdout = io.BytesIO(output)
        self.return_code = return_code
        self.pid = 12345

    def wait(self):
        return self.return_code

    def poll(self):
        return self.return_code


class PipelineAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.original_store = (app_module.api_store.db_path, app_module.api_store.output_dir)
        app_module.api_store.configure(
            str(self.root / "api.sqlite3"), str(self.root / "job-output")
        )
        self.original_users = app_module.user_manager.users.copy()
        self.user = app_module.User("api-user", "", is_admin=False)
        self.admin = app_module.User("api-admin", "", is_admin=True)
        app_module.user_manager.users[self.user.id] = self.user
        app_module.user_manager.users[self.admin.id] = self.admin
        app_module.app.config.update(
            TESTING=True,
            SECRET_KEY="api-test-secret-0123456789abcdef",
            ADMIN_DEFAULT_PASSWORD="api-test-admin-password",
            API_REQUIRE_HTTPS=True,
            API_SUBMIT_RATE_LIMIT=100,
            API_READ_RATE_LIMIT=100,
            API_RATE_WINDOW_SECONDS=60,
            PIPELINE_INPUT_ROOTS=(str(self.root),),
            PIPELINE_OUTPUT_ROOTS=(str(self.root),),
            PIPELINE_MAX_WORKERS=8,
            PIPELINE_MAX_THUMBNAIL_DIMENSION=4096,
        )
        self.client = app_module.app.test_client()
        self.run_token, self.run_record = app_module.api_store.create_token(
            self.user.id, "test runner", ["pipeline:read", "pipeline:run"], 90
        )
        self.read_token, _ = app_module.api_store.create_token(
            self.user.id, "read only", ["pipeline:read"], 90
        )
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()
        with app_module._pipeline_jobs_lock:
            app_module._pipeline_jobs.clear()
            app_module._pipeline_active_job_id = None

    def tearDown(self):
        with app_module._pipeline_jobs_lock:
            app_module._pipeline_jobs.clear()
            app_module._pipeline_active_job_id = None
        app_module.user_manager.users = self.original_users
        app_module.api_store.configure(*self.original_store)
        self.temporary_directory.cleanup()

    def headers(self, token=None, key=None):
        result = {"Authorization": f"Bearer {token or self.run_token}"}
        if key:
            result["Idempotency-Key"] = key
        return result

    def payload(self):
        return {"input_dir": str(self.input_dir), "output_dir": str(self.output_dir)}

    def create_durable_job(self, owner_id=None, status="succeeded", output="done\n"):
        job_id = "11111111-1111-4111-8111-111111111111"
        output_path = app_module.api_store.reserve_job(
            job_id, owner_id or self.user.id, {}, ["pipeline"], None, None, None
        )
        Path(output_path).write_text(output, encoding="utf-8")
        app_module.api_store.update_job(
            job_id,
            status=status,
            started_at=app_module._iso_utc(),
            completed_at=app_module._iso_utc() if status != "running" else None,
            return_code=0 if status == "succeeded" else None,
        )
        return job_id

    def test_create_job_and_replay_idempotent_request(self):
        process = FakeProcess()
        thread = mock.Mock()
        with mock.patch.object(app_module.subprocess, "Popen", return_value=process), mock.patch.object(
            app_module.threading, "Thread", return_value=thread
        ):
            response = self.client.post(
                "/api/v1/pipeline/jobs",
                json=self.payload(),
                headers=self.headers(key="submission-1"),
            )
            replay = self.client.post(
                "/api/v1/pipeline/jobs",
                json=self.payload(),
                headers=self.headers(key="submission-1"),
            )

        self.assertEqual(202, response.status_code)
        self.assertEqual("running", response.get_json()["data"]["status"])
        self.assertIn("Location", response.headers)
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(response.get_json()["data"]["id"], replay.get_json()["data"]["id"])
        thread.start.assert_called_once_with()

    def test_create_job_validates_request_and_idempotency_conflicts(self):
        no_json = self.client.post(
            "/api/v1/pipeline/jobs", data="x", headers=self.headers(key="bad-media")
        )
        missing_key = self.client.post(
            "/api/v1/pipeline/jobs", json=self.payload(), headers=self.headers()
        )
        unknown = self.client.post(
            "/api/v1/pipeline/jobs",
            json={**self.payload(), "mystery": True},
            headers=self.headers(key="unknown-field"),
        )
        self.assertEqual(415, no_json.status_code)
        self.assertEqual(422, missing_key.status_code)
        self.assertEqual(422, unknown.status_code)
        self.assertEqual("application/problem+json", unknown.content_type)

        over_limit = self.client.post(
            "/api/v1/pipeline/jobs",
            json={**self.payload(), "ocr_workers": 9},
            headers=self.headers(key="over-limit"),
        )
        self.assertEqual(422, over_limit.status_code)
        self.assertIn("must not exceed 8", over_limit.get_json()["detail"])

    def test_idempotency_key_rejects_a_different_payload(self):
        with mock.patch.object(app_module.subprocess, "Popen", return_value=FakeProcess()), mock.patch.object(
            app_module.threading, "Thread", return_value=mock.Mock()
        ):
            accepted = self.client.post(
                "/api/v1/pipeline/jobs",
                json=self.payload(),
                headers=self.headers(key="same-key"),
            )
            conflict = self.client.post(
                "/api/v1/pipeline/jobs",
                json={**self.payload(), "ocr_workers": 2},
                headers=self.headers(key="same-key"),
            )
        self.assertEqual(202, accepted.status_code)
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("idempotency_conflict", conflict.get_json()["code"])

    def test_job_status_enforces_ownership_and_allows_admin(self):
        job_id = self.create_durable_job(owner_id="someone-else")
        hidden = self.client.get(f"/api/v1/pipeline/jobs/{job_id}", headers=self.headers())
        admin_token, _ = app_module.api_store.create_token(
            self.admin.id, "admin", ["pipeline:read"], 90
        )
        visible = self.client.get(
            f"/api/v1/pipeline/jobs/{job_id}", headers=self.headers(admin_token)
        )
        self.assertEqual(404, hidden.status_code)
        self.assertEqual(200, visible.status_code)
        self.assertEqual("succeeded", visible.get_json()["data"]["status"])

    def test_job_output_supports_offsets_limits_and_errors(self):
        job_id = self.create_durable_job(output="alpha βeta\n")
        first = self.client.get(
            f"/api/v1/pipeline/jobs/{job_id}/output?offset=0&limit=6",
            headers=self.headers(),
        )
        body = first.get_json()["data"]
        second = self.client.get(
            f"/api/v1/pipeline/jobs/{job_id}/output?offset={body['next_offset']}",
            headers=self.headers(),
        )
        invalid = self.client.get(
            f"/api/v1/pipeline/jobs/{job_id}/output?offset=-1", headers=self.headers()
        )
        self.assertEqual("alpha ", body["output"])
        self.assertFalse(body["eof"])
        self.assertIn("βeta", second.get_json()["data"]["output"])
        self.assertEqual(422, invalid.status_code)

    def test_openapi_endpoint_serves_the_versioned_contract(self):
        response = self.client.get("/api/v1/openapi.json", headers=self.headers(self.read_token))
        contract = response.get_json()
        self.assertEqual(200, response.status_code)
        self.assertEqual("3.1.0", contract["openapi"])
        self.assertIn("/pipeline/jobs", contract["paths"])
        self.assertIn("bearerAuth", contract["components"]["securitySchemes"])
        properties = contract["components"]["schemas"]["PipelineRequest"]["properties"]
        self.assertEqual(8, properties["ocr_workers"]["maximum"])
        self.assertEqual(4096, properties["thumbnail_width"]["maximum"])

    def test_bearer_auth_scope_revocation_and_request_ids(self):
        missing = self.client.get("/api/v1/openapi.json")
        denied = self.client.post(
            "/api/v1/pipeline/jobs",
            json=self.payload(),
            headers=self.headers(self.read_token, "scope-test"),
        )
        app_module.api_store.revoke_token(self.run_record["token_id"])
        revoked = self.client.get("/api/v1/openapi.json", headers=self.headers())
        self.assertEqual(401, missing.status_code)
        self.assertIn("Bearer", missing.headers["WWW-Authenticate"])
        self.assertEqual(403, denied.status_code)
        self.assertEqual(401, revoked.status_code)
        self.assertRegex(missing.headers["X-Request-ID"], r"^[0-9a-f]{32}$")

    def test_expired_tokens_and_insecure_transport_are_rejected(self):
        expired_token, expired_record = app_module.api_store.create_token(
            self.user.id, "expired", ["pipeline:read"], 1
        )
        with app_module.api_store.connection() as connection:
            connection.execute(
                "UPDATE api_tokens SET expires_at=? WHERE token_id=?",
                ("2000-01-01T00:00:00Z", expired_record["token_id"]),
            )
        expired = self.client.get(
            "/api/v1/openapi.json", headers=self.headers(expired_token)
        )
        app_module.app.config["TESTING"] = False
        try:
            insecure = self.client.get(
                "/api/v1/openapi.json", headers=self.headers(self.read_token)
            )
        finally:
            app_module.app.config["TESTING"] = True
        self.assertEqual(401, expired.status_code)
        self.assertEqual(400, insecure.status_code)
        self.assertEqual("https_required", insecure.get_json()["code"])

    def test_rate_limit_is_enforced_per_token_and_bucket(self):
        app_module.app.config["API_READ_RATE_LIMIT"] = 1
        first = self.client.get("/api/v1/openapi.json", headers=self.headers(self.read_token))
        second = self.client.get("/api/v1/openapi.json", headers=self.headers(self.read_token))
        self.assertEqual(200, first.status_code)
        self.assertEqual(429, second.status_code)
        self.assertIn("Retry-After", second.headers)
        self.assertEqual("0", second.headers["X-RateLimit-Remaining"])

    def test_running_jobs_are_marked_interrupted_during_restart_recovery(self):
        job_id = self.create_durable_job(status="running", output="partial")
        app_module.api_store.update_job(job_id, launcher_pid=os.getpid())
        app_module.api_store.mark_stale_jobs_interrupted()
        record = app_module.api_store.get_job(job_id)
        self.assertEqual("interrupted", record["status"])
        self.assertIsNotNone(record["completed_at"])

    def test_browser_posts_require_csrf_but_api_bearer_posts_do_not(self):
        app_module.app.config["TESTING"] = False
        app_module.app.config["API_REQUIRE_HTTPS"] = False
        try:
            login_page = self.client.get("/login")
            token = re.search(
                rb'name="csrf_token" value="([^"]+)"', login_page.data
            ).group(1).decode()
            rejected = self.client.post(
                "/login", data={"username": "nobody", "password": "wrong"}
            )
            accepted_by_csrf = self.client.post(
                "/login",
                data={"username": "nobody", "password": "wrong", "csrf_token": token},
            )
            with mock.patch.object(app_module.subprocess, "Popen", return_value=FakeProcess()), mock.patch.object(
                app_module.threading, "Thread", return_value=mock.Mock()
            ):
                api_response = self.client.post(
                    "/api/v1/pipeline/jobs",
                    json=self.payload(),
                    headers=self.headers(key="csrf-exempt-api"),
                )
        finally:
            app_module.app.config["TESTING"] = True
            app_module.app.config["API_REQUIRE_HTTPS"] = True
        self.assertEqual(400, rejected.status_code)
        self.assertEqual(200, accepted_by_csrf.status_code)
        self.assertEqual(202, api_response.status_code)


class APITokenCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.original_store = (app_module.api_store.db_path, app_module.api_store.output_dir)
        app_module.api_store.configure(str(self.root / "api.sqlite3"), str(self.root / "output"))
        self.original_users = app_module.user_manager.users.copy()
        app_module.user_manager.users["cli-user"] = app_module.User("cli-user", "")
        self.runner = app_module.app.test_cli_runner()

    def tearDown(self):
        app_module.user_manager.users = self.original_users
        app_module.api_store.configure(*self.original_store)
        self.temporary_directory.cleanup()

    def test_create_list_rotate_and_revoke_tokens(self):
        created = self.runner.invoke(
            args=["api-token", "create", "cli-user", "--label", "partner"]
        )
        self.assertEqual(0, created.exit_code, created.output)
        raw_token = next(line for line in created.output.splitlines() if line.startswith("lc_pat_"))
        token_id = raw_token.split(".", 1)[0].removeprefix("lc_pat_")
        database_bytes = (self.root / "api.sqlite3").read_bytes()
        self.assertNotIn(raw_token.encode(), database_bytes)

        listed = self.runner.invoke(args=["api-token", "list", "--user", "cli-user"])
        self.assertIn(token_id, listed.output)
        self.assertNotIn(raw_token, listed.output)

        rotated = self.runner.invoke(args=["api-token", "rotate", token_id])
        self.assertEqual(0, rotated.exit_code, rotated.output)
        self.assertIn("replacement ID", rotated.output)
        self.assertIsNone(app_module.api_store.authenticate_token(raw_token))


if __name__ == "__main__":
    unittest.main()
