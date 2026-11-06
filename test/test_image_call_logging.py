from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from services.log_service import LogService


class LegacyImageCallLoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.logs = LogService(Path(directory.name) / "logs.jsonl")
        self.detail = {"key_id": "synthetic-owner", "endpoint": "/v1/images/generations", "image_task_id": "job-1"}

    def status(self, **detail):
        self.logs.add("call", "生图任务状态", {**self.detail, "image_child_id": "child-1",
                      "phase": "output_saved", "retry_count": 1, "outcome": "success", **detail})

    def call(self, **detail):
        self.logs.add("call", "文生图调用完成", {**self.detail, "status": "success", "duration_ms": 1234,
                      "urls": ["/images/synthetic.png"], "request_params": {"prompt": "draw"}, **detail})

    def test_historical_pairs_merge_before_limit_without_rewriting_history(self):
        self.call(image_task_id="older")
        self.status()
        self.call()
        before = self.logs.path.read_bytes()
        items = self.logs.list(type="call", limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual([item["summary"] for item in items], ["文生图调用完成"] * 2)
        detail = items[0]["detail"]
        self.assertEqual(detail["duration_ms"], 1234)
        self.assertEqual(detail["urls"], ["/images/synthetic.png"])
        self.assertEqual(detail["request_params"], {"prompt": "draw"})
        self.assertEqual(detail["retry_count"], 1)
        self.assertEqual(detail["image_tasks"][0]["image_child_id"], "child-1")
        self.assertEqual(self.logs.path.read_bytes(), before)

    def test_all_children_merge_and_latest_diagnostic_wins_even_if_written_after_call(self):
        self.status(phase="rejected", outcome="queued")
        self.call()
        self.status()
        self.status(image_child_id="child-2", outcome="error", error_category="transient")
        items = self.logs.list()
        self.assertEqual(len(items), 1)
        children = {child["image_child_id"]: child for child in items[0]["detail"]["image_tasks"]}
        self.assertEqual(children["child-1"]["outcome"], "success")
        self.assertEqual(children["child-2"]["error_category"], "transient")

    def test_distinct_calls_and_orphan_or_web_task_logs_are_preserved(self):
        self.status()
        self.call()
        self.call()  # A separate idempotent HTTP read remains a real call.
        self.status(key_id="other-owner")
        self.status(image_task_id="different-job")
        self.status(endpoint="/api/image-tasks/generations")
        self.call(image_task_id="")
        self.assertEqual(len(self.logs.list(type="call")), 6)

    def test_date_and_type_filters_still_apply_to_the_actual_call(self):
        self.status()
        self.call()
        rows = [json.loads(line) for line in self.logs.path.read_text(encoding="utf-8").splitlines()]
        rows[0]["time"] = "2026-10-08 23:59:59+08:00"
        rows[1]["time"] = "2026-10-09 00:00:00+08:00"
        self.logs.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        self.assertEqual(len(self.logs.list(start_date="2026-10-09", end_date="2026-10-09")), 1)
        self.assertEqual(self.logs.list(end_date="2026-10-08"), [])
        self.assertEqual(self.logs.list(type="account"), [])

    def test_deleting_visible_call_also_removes_its_hidden_diagnostics(self):
        self.status(phase="rejected", outcome="queued")
        self.status()
        self.call()
        self.status(image_task_id="unrelated")
        target = next(item for item in self.logs.list() if item["summary"] == "文生图调用完成")
        self.logs.delete([target["id"]])
        remaining = self.logs.list()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["detail"]["image_task_id"], "unrelated")


class RuntimeImageDiagnosticsTests(unittest.TestCase):
    def test_real_store_counts_history_timings_and_owner_isolation(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from services.generation_store import GenerationStore
        from services.image_call_logging import runtime_call_details
        from contextlib import ExitStack
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            store = GenerationStore(Path(directory) / 'tasks.sqlite')
            stack.callback(store.db.close)
            store.db.execute("INSERT INTO jobs(id,owner,identity,endpoint,fingerprint,payload,model,channel,created,deadline) VALUES ('j','o','{}','/v1/images/generations','f','{}','alias','web',10,100)")
            for ordinal, status in enumerate(('success', 'error')):
                store.db.execute("INSERT INTO tasks(id,job_id,ordinal,owner,status,phase,created,started,finished,updated,account_id,attempt,attempted_accounts) VALUES (?, 'j', ?, 'o', ?, ?, 10,12,15,15,'last-account',2,?)",
                                 (str(ordinal), ordinal, status, status, '["first-account"]'))
                store.db.execute("INSERT INTO settlements(task_id,owner,images,released,outcome,created) VALUES (?, 'o', ?, 1, ?, 15)", (str(ordinal), 1 if ordinal == 0 else 0, status))
                store.db.execute("INSERT INTO events(task_id,phase,category,attempt,outcome,created) VALUES (?, 'rejected','rate_limited',1,'retry',11)", (str(ordinal),))
            store.db.execute("INSERT INTO key_usage(owner,used,reserved) VALUES ('o',1,0)")
            with patch('services.generation_runtime.get_generation_runtime', return_value=SimpleNamespace(store=store)):
                detail = runtime_call_details('j', 'o')
                self.assertEqual(detail.get('requested_count'), 2)
                self.assertEqual(detail['output_count'], 1)
                self.assertTrue(detail['partial_success'])
                self.assertEqual(detail['queue_duration_ms'], 2000)
                self.assertEqual(detail['execution_duration_ms'], 3000)
                child = detail['image_tasks'][0]
                self.assertEqual(child['account_id'], 'last-account')
                self.assertEqual(child['attempted_accounts'], ['first-account'])
                self.assertEqual(child['attempts'][0]['error_category'], 'rate_limited')
                self.assertIsNone(child['attempts'][0]['account_id'])
                self.assertIsNone(child['attempts'][0]['error_code'])
                self.assertIsNone(detail.get('quota_snapshot'))
                self.assertEqual(runtime_call_details('j', 'someone-else'), {})

    def test_new_events_keep_account_and_diagnostics_without_backfill(self):
        from services.generation_store import GenerationStore
        from services.generation_context import ExecutionCheckpoint, execution_checkpoint, checkpoint
        from services.diagnostic_context import upstream_request
        from services.image_call_logging import runtime_call_details
        from unittest.mock import patch
        from types import SimpleNamespace
        from contextlib import ExitStack
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            store = GenerationStore(Path(directory) / 'tasks.sqlite')
            stack.callback(store.close)
            store.db.execute("INSERT INTO jobs(id,owner,identity,endpoint,fingerprint,payload,model,channel,created,deadline) VALUES ('j','o','{}','/v1/images/generations','f','{}','alias','web',10,100)")
            store.db.execute("INSERT INTO tasks(id,job_id,ordinal,owner,status,phase,created,updated) VALUES ('t','j',0,'o','running','preparing',10,10)")
            store.db.execute("INSERT INTO events(task_id,phase,attempt,created) VALUES ('t','legacy',1,10)")
            ctx = ExecutionCheckpoint(persist=lambda **values: store.checkpoint('t', **values), raw_writer=lambda _: None)
            token = execution_checkpoint.set(ctx)
            try:
                checkpoint('account_selected', account_id='a1', attempt=1)
                upstream_request('real-model', 'web', {'pool_account_id': 'a1'})
                checkpoint('rejected', error_category='rate_limited', error_code='rate_limit', outcome='retry')
                checkpoint('account_selected', account_id='a2', attempt=2)
                upstream_request('real-model-2', 'web', {'pool_account_id': 'a2'})
            finally:
                execution_checkpoint.reset(token)
            with patch('services.generation_runtime.get_generation_runtime', return_value=SimpleNamespace(store=store)):
                detail = runtime_call_details('j', 'o')
            self.assertEqual(detail.get('actual_model'), 'real-model-2')
            self.assertEqual(detail['actual_model_source'], 'upstream_request')
            self.assertEqual(detail['attempts'][0]['account_id'], 'a1')
            self.assertEqual(detail['attempts'][0]['error_code'], 'rate_limit')
            self.assertEqual(detail['attempts'][1]['account_id'], 'a2')
            self.assertIsNone(detail['image_tasks'][0]['events'][0]['account_id'])
            self.assertEqual(set(detail['attempted_accounts']), {'a1', 'a2'})
            from services.generation_runtime import GenerationRuntime
            from services.log_service import LogService
            store.db.execute("UPDATE jobs SET endpoint='/api/image-tasks/edits' WHERE id='j'")
            store.db.execute("UPDATE tasks SET status='success' WHERE id='t'")
            runtime = GenerationRuntime.__new__(GenerationRuntime)
            runtime.store = store
            logs = LogService(Path(directory) / 'logs.jsonl')
            with patch('services.log_service.log_service', logs):
                runtime._log_task('t', 'output_saved')
            self.assertEqual(logs.list()[0]['detail'].get('actual_model'), 'real-model-2')
            store.db.execute("UPDATE tasks SET status='running' WHERE id='t'")
            store.finish('t', status='uncertain', category='transient', code='connection_lost')
            last = store.rows("SELECT category,account_id,error_code FROM events ORDER BY id DESC LIMIT 1")[0]
            self.assertEqual(last['error_code'], 'connection_lost')
            self.assertEqual(last['account_id'], 'a2')




class CreativeImageInputLoggingTests(unittest.TestCase):
    def test_frontend_edit_submission_logs_safe_inputs_and_missing_file_keeps_log(self):
        import base64
        import hashlib
        from test.test_generation_api import GenerationAPITests
        for transport in ('json', 'multipart'):
            with self.subTest(transport=transport):
                fixture = GenerationAPITests(methodName='runTest')
                fixture.setUp()
                try:
                    fields = {'client_task_id': 'input-summary', 'prompt': 'synthetic reference', 'model': 'gpt-image-2'}
                    if transport == 'json':
                        response = fixture.client.post('/api/image-tasks/edits', headers=fixture.admin,
                            json={**fields, 'images': [{'b64_json': fixture.png}]})
                    else:
                        response = fixture.client.post('/api/image-tasks/edits', headers=fixture.admin, data=fields,
                            files={'image': ('reference.png', base64.b64decode(fixture.png), 'image/png')})
                    self.assertEqual(response.status_code, 200, response.text)
                    fixture.wait_for(lambda: bool(fixture.logs.list(type='call')))
                    detail = fixture.logs.list(type='call')[0]['detail']
                    self.assertIn('images', detail['request_params'])
                    summary = detail['request_params']['images'][0]
                    self.assertEqual(summary['sha256'], hashlib.sha256(base64.b64decode(fixture.png)).hexdigest())
                    self.assertEqual((summary['width'], summary['height']), (16, 16))
                    self.assertTrue(summary['content_omitted'])
                    self.assertNotIn(fixture.png, fixture.logs.path.read_text(encoding='utf-8'))
                    self.assertNotIn('request_params', response.json())
                    fixture.runtime.stop()
                    path = fixture.runtime._private_path(detail['image_task_id'], 'inputs.json')
                    original = path.read_bytes()
                    # Reading diagnostics does not rewrite the existing private input file.
                    fixture.runtime._log_task(detail['image_child_id'], 'output_saved')
                    self.assertEqual(path.read_bytes(), original)
                    for invalid in (None, '{bad json', '[]'):
                        if invalid is None:
                            path.unlink()
                        else:
                            path.write_text(invalid, encoding='utf-8')
                        before = len(fixture.logs.list())
                        fixture.runtime._log_task(detail['image_child_id'], 'output_saved')
                        self.assertEqual(len(fixture.logs.list()), before + 1)
                        latest = fixture.logs.list()[0]['detail']
                        self.assertEqual(latest['request_params']['prompt'], fields['prompt'])
                        self.assertEqual(latest['model'], fields['model'])
                        self.assertNotIn('images', latest['request_params'])
                finally:
                    fixture.tearDown()

    def test_batch_actual_model_requires_every_child_to_agree(self):
        from services.image_call_logging import with_task_diagnostics
        first = {'actual_model': 'actual', 'actual_model_source': 'upstream_request'}
        for second, expected_model, expected_source in (
                (dict(first), 'actual', 'upstream_request'),
                ({'actual_model': 'different', 'actual_model_source': 'upstream_request'}, None, None),
                ({}, None, None),
                ({'actual_model': 'actual', 'actual_model_source': 'upstream_response'}, 'actual', None)):
            with self.subTest(second=second):
                children = [first, second]
                merged = with_task_diagnostics({}, children)
                self.assertEqual(merged.get('actual_model'), expected_model)
                self.assertEqual(merged.get('actual_model_source'), expected_source)
                self.assertEqual(merged['image_tasks'], children)


class RetryEvidenceIsolationTests(unittest.TestCase):
    def setUp(self):
        from services.generation_runtime import GenerationRuntime
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = GenerationRuntime(Path(directory.name), executor=lambda *args: None)
        self.addCleanup(self.runtime.store.close)
        self.store = self.runtime.store
        self.store.db.execute("INSERT INTO jobs(id,owner,identity,endpoint,fingerprint,payload,model,channel,created,deadline) VALUES ('j','o','{}','/v1/images/generations','f',?, 'alias','web',10,100)",
                              (json.dumps({'model': 'gpt-image-2', 'prompt': 'draw', 'n': 1}),))
        self.store.db.execute("INSERT INTO tasks(id,job_id,ordinal,owner,status,phase,created,updated) VALUES ('t','j',0,'o','queued','queued',10,10)")
        self.store.db.execute("INSERT INTO key_usage(owner,reserved) VALUES ('o',1)")

    def context(self):
        from services.generation_context import ExecutionCheckpoint
        row = self.store.task('t', include_output=False)
        return ExecutionCheckpoint(persist=lambda **values: self.store.checkpoint('t', **values),
            raw_writer=lambda _: None, account_id=row['account_id'] or '', phase=row['phase'], attempt=row['attempt'],
            details={'diagnostics': json.loads(row.get('diagnostics') or '{}')})

    def record_first(self, context):
        from services.generation_context import execution_checkpoint, checkpoint
        from services.diagnostic_context import trace_upload, upstream_request
        token = execution_checkpoint.set(context)
        try:
            checkpoint('account_selected', account_id='A', attempt=1)
            trace_upload(lambda: {'file_id': 'synthetic'})()
            upstream_request('model-A', 'web', {'pool_account_id': 'A'}, input_count=1, tool_model='tool-A')
            checkpoint('rejected', error_category='rate_limited', error_code='rate_limit', outcome='retry')
        finally:
            execution_checkpoint.reset(token)

    def assert_isolated(self, account):
        from services.image_call_logging import runtime_call_details
        detail = runtime_call_details('j', 'o', store=self.store)
        for record in (detail, detail['image_tasks'][0], detail['attempts'][-1]):
            self.assertEqual(record['account_id'], account)
            for field in ('actual_model', 'actual_model_source', 'actual_tool_model', 'actual_tool_model_source', 'submitted_input_count'):
                self.assertIsNone(record.get(field), (field, record))
            self.assertFalse(record.get('input_uploads'))
        self.assertEqual(len(detail['attempts']), 2)
        first = detail['attempts'][0]
        self.assertEqual(first['account_id'], 'A')
        self.assertEqual(first['actual_model'], 'model-A')
        self.assertEqual(first['submitted_input_count'], 1)
        self.assertEqual(first['input_uploads'][0]['status'], 'confirmed')
        self.assertEqual(first['error_code'], 'rate_limit')

    def test_queued_retry_fails_before_execution_without_reusing_old_evidence(self):
        from services.generation_errors import GenerationRuntimeError
        for account, attempt in (('B', 2), ('A', 2), ('A', 1)):
            with self.subTest(account=account, attempt=attempt):
                self.store.db.execute("UPDATE tasks SET status='queued',phase='queued',account_id=NULL,attempt=0,diagnostics=NULL,recovery='' WHERE id='t'")
                self.store.db.execute("DELETE FROM events")
                self.store.db.execute("DELETE FROM settlements")
                self.store.db.execute("UPDATE tasks SET reservation=1 WHERE id='t'")
                self.store.db.execute("UPDATE key_usage SET reserved=1 WHERE owner='o'")
                self.store.claim('t', account_id='A', attempt=1)
                self.record_first(self.context())
                self.store.requeue_attempt('t')
                self.store.claim('t', account_id=account, attempt=attempt)
                def fail(*args):
                    raise GenerationRuntimeError('synthetic early failure', 'invalid_image_request', 400)
                self.runtime.execute = fail
                self.runtime._run('t')
                self.assert_isolated(account)
                self.store.db.execute("DELETE FROM settlements")
                self.store.db.execute("UPDATE tasks SET reservation=1 WHERE id='t'")
                self.store.db.execute("UPDATE key_usage SET reserved=1 WHERE owner='o'")

    def test_same_account_in_process_new_attempt_is_isolated(self):
        from services.generation_context import execution_checkpoint, checkpoint
        from services.diagnostic_context import account_evidence
        self.store.claim('t', account_id='A', attempt=1)
        context = self.context()
        self.record_first(context)
        token = execution_checkpoint.set(context)
        try:
            checkpoint('account_selected', account_id='A', attempt=1)
            account_evidence({'pool_account_id': 'A'}, 'web')
            checkpoint('rejected', error_code='before_submit', outcome='stop')
        finally:
            execution_checkpoint.reset(token)
        self.assert_isolated('A')

    def test_result_recovery_keeps_same_attempt_evidence(self):
        from services.image_call_logging import runtime_call_details
        self.store.claim('t', account_id='A', attempt=1)
        self.record_first(self.context())
        self.store.db.execute("UPDATE tasks SET status='queued',phase='submitted',recovery='recovering_result',conversation_id='conversation' WHERE id='t'")
        self.store.claim('t', account_id='A')
        detail = runtime_call_details('j', 'o', store=self.store)
        self.assertEqual(detail['actual_model'], 'model-A')
        self.assertEqual(detail['input_uploads'][0]['status'], 'confirmed')
        self.assertEqual(len(detail['attempts']), 1)


if __name__ == "__main__":
    unittest.main()
