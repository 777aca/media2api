from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.log_service import LogService, LoggedCall


class LogDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.logs = LogService(Path(temporary.name) / 'logs.jsonl')

    def test_filter_before_pagination_and_legacy_signature(self):
        for index in range(245):
            self.logs.add('call', f'summary {index}', {'status': 'failed' if index < 23 else 'success',
                'requested_model': 'requested', 'actual_model': 'actual', 'channel': 'web',
                'account_id': 'stable-id', 'account_email': 'User@example.test', 'task_type': 'image',
                'request_id': f'req-{index}'})
        self.assertTrue(callable(getattr(self.logs, 'query', None)), 'missing server-side query')
        result = self.logs.query(status='failed', channel='web', model='actual', account='USER@', task_type='image', page=2, page_size=20)
        self.assertEqual(result['total'], 23)
        self.assertEqual(len(result['items']), 3)
        self.assertEqual(result['items'][-1]['detail']['request_id'], 'req-0')
        self.assertEqual(self.logs.query(q='req-0')['total'], 1)
        self.assertEqual(self.logs.query(account='stable-id', model='requested')['total'], 245)
        self.assertEqual(len(self.logs.list('call', '', '', 2)), 2)
        for values in ({'page': 0}, {'page_size': 0}, {'page_size': 101}):
            with self.assertRaises(ValueError):
                self.logs.query(**values)

    def test_unknown_model_and_counts_are_not_inferred_from_urls(self):
        call = LoggedCall({}, '/v1/chat/completions', 'alias', 'chat', request_params={'n': 2})
        with patch('services.log_service.log_service', self.logs):
            call.log('done', {'model': 'alias', 'usage': {'total_tokens': 8}, 'url': 'https://example.test'})
        detail = self.logs.list()[0]['detail']
        self.assertEqual(detail.get('requested_model'), 'alias')
        self.assertIsNone(detail['actual_model'])
        self.assertIsNone(detail['output_count'])
        self.assertEqual(detail['requested_count'], 2)
        self.assertEqual(detail['usage'], {'total_tokens': 8})
        self.assertEqual(detail['task_type'], 'chat')
        self.assertTrue(detail['request_id'])

    def test_stream_first_content_usage_and_internal_fields(self):
        call = LoggedCall({}, '/v1/chat/completions', 'alias', 'chat')
        items = [{'choices': [{'delta': {'role': 'assistant'}}]},
                 {'choices': [{'delta': {'content': 'hello'}}], '_account_email': 'synthetic@test'},
                 {'usage': {'total_tokens': 12}}]
        with patch('services.log_service.log_service', self.logs):
            public = list(call.stream(iter(items)))
        detail = self.logs.list()[0]['detail']
        self.assertIsNotNone(detail.get('first_token_ms'))
        self.assertEqual(detail['usage'], {'total_tokens': 12})
        self.assertNotIn('_account_email', str(public))

    def test_empty_stream_has_no_first_token(self):
        call = LoggedCall({}, '/v1/chat/completions', 'alias', 'chat')
        with patch('services.log_service.log_service', self.logs):
            list(call.stream(iter([{'choices': [{'delta': {'role': 'assistant'}}]}])))
        self.assertIsNone(self.logs.list()[0]['detail'].get('first_token_ms'))

    def test_non_wrapper_call_contract_preserves_unknowns(self):
        self.logs.add('call', 'ppt', {'endpoint': '/v1/ppt/generations', 'model': 'alias', 'status': 'success'})
        detail = self.logs.list()[0]['detail']
        self.assertEqual(detail.get('task_type'), 'ppt')
        self.assertIsNone(detail['request_id'])
        self.assertIsNone(detail['actual_model'])
        self.assertIsNone(detail['output_count'])

    def test_api_pagination_validation_and_filters(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api import system
        self.logs.add('call', 'needle', {'status': 'failed', 'account_id': 'stable'})
        app = FastAPI()
        app.include_router(system.create_router('test'))
        with patch.object(system, 'log_service', self.logs), patch.object(system, 'require_admin'), TestClient(app) as client:
            result = client.get('/api/logs?status=failed&account=stable&q=needle').json()
            self.assertEqual(result['total'], 1)
            self.assertEqual((result['page'], result['page_size']), (1, 20))
            for query in ('page=0', 'page_size=0', 'page_size=101'):
                self.assertEqual(client.get('/api/logs?' + query).status_code, 422)

    def test_late_failure_keeps_usage(self):
        call = LoggedCall({}, '/v1/chat/completions', 'alias', 'chat')
        def stream():
            yield {'usage': {'total_tokens': 5}}
            raise RuntimeError('synthetic')
        with patch('services.log_service.log_service', self.logs), self.assertRaises(RuntimeError):
            list(call.stream(stream()))
        self.assertEqual(self.logs.list()[0]['detail']['usage'], {'total_tokens': 5})

    def test_legacy_diagnostics_fill_null_defaults(self):
        self.logs.add('call', '生图任务状态', {'key_id': 'owner', 'endpoint': '/v1/images/generations',
            'image_task_id': 'job', 'image_child_id': 'child', 'channel': 'web', 'phase': 'rejected'})
        self.logs.add('call', 'done', {'key_id': 'owner', 'endpoint': '/v1/images/generations', 'image_task_id': 'job'})
        self.assertEqual(self.logs.list()[0]['detail'].get('channel'), 'web')

class ExecutionEvidenceTests(unittest.TestCase):
    def test_filter_alias_partial_and_model_substring(self):
        from services.log_diagnostics import matches_diagnostics
        row = {'detail': {'channel': 'chatgpt_web', 'actual_model': 'GPT-5-5', 'partial_success': True}}
        self.assertTrue(matches_diagnostics(row, channel='web', model='5-5', status='partial'))
        self.assertTrue(matches_diagnostics({'detail': {'channel': 'grok_web'}}, channel='grok'))

    def test_web_model_and_upload_confirmation_evidence(self):
        import base64
        from io import BytesIO
        from unittest import mock
        from PIL import Image
        from services.openai_backend_api import OpenAIBackendAPI
        from services.diagnostic_context import diagnostic_scope
        image = BytesIO(); Image.new('RGB', (2, 3), 'red').save(image, format='PNG')
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.base_url = 'https://example.invalid'
        backend.user_agent = 'test'
        backend.account = {'pool_account_id': 'stable', 'email': 'test@example.test', 'access_token': 'SECRET'}
        backend.session = mock.Mock()
        backend.session.post.return_value.status_code = 200
        backend.session.post.return_value.json.return_value = {'file_id': 'file-synthetic', 'upload_url': 'https://example.invalid/?token=SECRET'}
        backend.session.put.return_value.status_code = 200
        evidence = {}
        with diagnostic_scope(evidence), mock.patch.object(backend, '_headers', return_value={}), mock.patch.object(backend, '_image_headers', return_value={}), mock.patch.object(backend, '_image_model_settings', return_value=('actual-backend', '')):
            ref = backend._upload_image(base64.b64encode(image.getvalue()).decode())
            backend._start_image_generation('draw', mock.Mock(), 'secret-conduit', 'alias', [ref])
        self.assertEqual(evidence['actual_model'], 'actual-backend')
        self.assertEqual(evidence['actual_model_source'], 'upstream_request')
        self.assertEqual(evidence['input_uploads'][0]['status'], 'confirmed')
        self.assertEqual(evidence['submitted_input_count'], 1)
        self.assertEqual(len(evidence['input_uploads'][0]['sha256']), 64)
        self.assertNotIn('SECRET', str(evidence))
        self.assertEqual(evidence['account_id'], 'stable')
        evidence = {}
        backend.session.put.side_effect = RuntimeError('SECRET signed url')
        with diagnostic_scope(evidence), mock.patch.object(backend, '_headers', return_value={}), self.assertRaises(RuntimeError):
            backend._upload_image(base64.b64encode(image.getvalue()).decode())
        self.assertEqual(evidence['input_uploads'][0]['status'], 'failed')
        self.assertEqual(evidence['input_uploads'][0]['phase'], 'upload_bytes')
        self.assertNotIn('SECRET', str(evidence))

    def test_codex_and_grok_models_at_transport(self):
        from unittest import mock
        from types import SimpleNamespace
        from services.diagnostic_context import diagnostic_scope
        from services.codex_image_service import generate_codex_image
        from services.providers.grok_web import GrokWeb
        backend = SimpleNamespace(access_token='SECRET', account={'pool_account_id': 'codex-id'},
                                  base_url='https://example.invalid', session=mock.Mock())
        backend.session.post.return_value.status_code = 200
        backend.session.post.return_value.json.return_value = {}
        evidence = {}
        with diagnostic_scope(evidence), mock.patch('services.codex_image_service.codex_headers', return_value={}), mock.patch('services.codex_image_service.parse_codex_image_response', return_value=[{'b64_json': 'test'}]):
            generate_codex_image(backend, model='gpt-image-2.5-flare', prompt='draw')
        self.assertEqual(evidence.get('actual_model'), backend.session.post.call_args.kwargs['json']['model'])
        client = GrokWeb.__new__(GrokWeb)
        client.account = {'id': 'grok-id', 'sso_token': 'SECRET'}
        client.settings = {'image_timeout': 1}
        client.upload = mock.Mock(return_value='file-id')
        client.request = mock.Mock(side_effect=RuntimeError('stop before network'))
        evidence = {}
        with diagnostic_scope(evidence), self.assertRaises(RuntimeError):
            client.image(SimpleNamespace(model='grok-imagine-image-edit', size='1024x1024', images=['synthetic'], prompt='draw'))
        self.assertEqual(evidence.get('actual_model'), 'imagine-image-edit')
        self.assertEqual(evidence['actual_model_source'], 'upstream_request')
        self.assertEqual(evidence['account_id'], 'grok-id')
        self.assertNotIn('SECRET', str(evidence))

    def test_quota_requires_account_cache_timestamp_and_hides_old_key_quota(self):
        from services.diagnostic_context import diagnostic_scope, account_evidence
        evidence = {}
        with diagnostic_scope(evidence):
            account_evidence({'pool_account_id': 'a', 'quota': 9}, 'web')
        self.assertIsNone(evidence.get('quota_snapshot'))
        with diagnostic_scope(evidence):
            account_evidence({'pool_account_id': 'a', 'quota': 9, 'quota_updated_at': 123}, 'web')
        self.assertEqual(evidence['quota_snapshot']['scope'], 'pool_account_image')
        displayed = LogService._display_item({'detail': {'quota_snapshot': {'scope': 'api_key_image', 'used': 3}}})
        self.assertIsNone(displayed['detail'].get('quota_snapshot'))

    def test_parent_status_and_null_legacy_accounts(self):
        from services.log_diagnostics import matches_diagnostics
        for parent in ({'status': 'partial'}, {'status': 'success', 'partial_success': True},
                       {'status': 'failed', 'requested_count': 3, 'output_count': 1}):
            row = {'detail': {**parent, 'attempted_accounts': None, 'image_tasks': [{'status': 'success'}]}}
            self.assertTrue(matches_diagnostics(row, status='partial'))
            self.assertFalse(matches_diagnostics(row, status='success'))
            self.assertFalse(matches_diagnostics(row, status='failed'))
            self.assertFalse(matches_diagnostics(row, account='missing'))
        self.assertFalse(matches_diagnostics({'detail': {'status': 'failed', 'image_tasks': [{'status': 'success'}]}}, status='success'))

    def test_context_crosses_handler_and_stream_without_response_leaks(self):
        import asyncio
        from services.diagnostic_context import upstream_request
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        logs = LogService(Path(temporary.name) / 'logs.jsonl')
        def handler():
            upstream_request('handler-model', 'web', {'pool_account_id': 'stable'})
            def stream():
                upstream_request('stream-model', 'web', {'pool_account_id': 'stable'})
                yield {'choices': [{'delta': {'content': 'hello'}}]}
            return stream()
        call = LoggedCall({}, '/v1/chat/completions', 'alias', 'test')
        async def run():
            response = await call.run(handler)
            return [chunk async for chunk in response.body_iterator]
        with patch('services.log_service.log_service', logs):
            public = asyncio.run(run())
        self.assertNotIn('actual_model', str(public))
        self.assertEqual(logs.list()[0]['detail']['actual_model'], 'stream-model')

    def test_image_task_id_overrides_response_endpoint_type(self):
        from services.log_diagnostics import normalize_call_detail
        self.assertEqual(normalize_call_detail({'endpoint': '/v1/responses', 'image_task_id': 'job'})['task_type'], 'image')
