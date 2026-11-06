"""Execution-only evidence. Never accepts request bodies, headers or exception text."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import re
import time
from uuid import uuid4

_current = ContextVar('call_diagnostics', default=None)
_upload = ContextVar('input_upload', default=None)


@contextmanager
def diagnostic_scope(detail):
    token = _current.set(detail)
    try:
        yield
    finally:
        _current.reset(token)


def invoke(detail, function, *args):
    with diagnostic_scope(detail):
        return function(*args)


def iterate(detail, items):
    iterator = iter(items)
    while True:
        with diagnostic_scope(detail):
            try:
                item = next(iterator)
            except StopIteration:
                return
        yield item


def emit(**values):
    from services.generation_context import execution_checkpoint
    execution = execution_checkpoint.get()
    detail = execution.details.setdefault('diagnostics', {}) if execution else _current.get()
    if detail is None:
        return
    detail.update(values)
    if execution:
        # Diagnostic persistence must not change the retry/submission state machine.
        try:
            execution.persist(diagnostics=detail, diagnostic_event=values,
                              diagnostic_account_id=execution.account_id or None)
        except Exception:
            pass


def new_attempt_diagnostics(previous, account_id, attempt=0, *, pending=False):
    """A new execution, including the same account, owns a fresh evidence snapshot.

    Diagnostic numbering is independent of the scheduler's distinct-account count.
    Historical evidence remains in immutable events, never in the new snapshot.
    """
    previous_number = previous.get("_attempt_number", attempt if previous else 0)
    return {
        "_attempt_number": max(previous_number + 1, attempt, 1),
        "_attempt_pending": pending,
        "account_id": account_id or None, "account_email": None,
        "actual_model": None, "actual_model_source": None,
        "actual_tool_model": None, "actual_tool_model_source": None,
        "submitted_input_count": None, "input_uploads": [], "quota_snapshot": None,
    }


def account_evidence(account, channel):
    if not isinstance(account, dict):
        return
    stable = account.get('pool_account_id') or (account.get('id') if channel == 'grok_web' else None)
    emit(account_id=stable, account_email=account.get('email') or None, channel=channel, quota_snapshot=None)
    # Legacy normalized quota=0 is not upstream evidence; require a dated cache.
    quota = account.get('quota')
    updated = account.get('quota_updated_at')
    if type(quota) in (int, float) and type(updated) in (int, float) and updated > 0:
        emit(quota_snapshot={'scope': 'pool_account_image', 'type': 'remaining', 'unit': 'images', 'account_id': stable,
                             'remaining': quota, 'updated_at': updated})

    if channel == 'grok_web' and type(account.get('last_synced')) in (int, float) and account['last_synced'] > 0 and isinstance(account.get('quotas'), dict):
        dimensions = {key: {field: value for field, value in quota.items() if field in ('remaining', 'total', 'reset_at') and type(value) in (int, float)}
                      for key, quota in account['quotas'].items() if key in ('image_pro', 'image_edit') and isinstance(quota, dict)}
        if dimensions:
            emit(quota_snapshot={'scope': 'pool_account_image', 'type': 'remaining', 'unit': 'images', 'account_id': stable,
                                 'dimensions': dimensions, 'updated_at': account['last_synced']})


def upstream_request(model, channel, account=None, *, input_count=None, tool_model=None):
    account_evidence(account, channel)
    if isinstance(model, str) and model:
        emit(actual_model=model, actual_model_source='upstream_request', channel=channel)
    if isinstance(tool_model, str) and tool_model:
        emit(actual_tool_model=tool_model, actual_tool_model_source='upstream_request')
    if type(input_count) is int:
        emit(submitted_input_count=input_count)


def safe_error(exc):
    code = getattr(exc, 'code', None)
    return {'error_code': code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code) else None,
            'error_type': type(exc).__name__}


def upload_stage(phase, data=None):
    current = _upload.get()
    if current is None:
        return
    current['phase'] = phase
    if isinstance(data, bytes):
        current.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    _save_upload(current)


def _save_upload(current):
    from services.generation_context import execution_checkpoint
    execution = execution_checkpoint.get()
    detail = execution.details.get('diagnostics', {}) if execution else (_current.get() or {})
    uploads = [dict(item) for item in detail.get('input_uploads', []) if item['upload_id'] != current['upload_id']]
    emit(input_uploads=[*uploads, dict(current)])


def trace_upload(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        current = {'upload_id': uuid4().hex, 'status': 'started', 'phase': 'validate'}
        token = _upload.set(current)
        started = time.monotonic()
        try:
            _save_upload(current)
            result = function(*args, **kwargs)
            current['status'] = 'confirmed'
            return result
        except Exception as exc:
            current.update(status='failed', **safe_error(exc))
            raise
        finally:
            current['duration_ms'] = round((time.monotonic() - started) * 1000)
            _save_upload(current)
            _upload.reset(token)
    return wrapped
