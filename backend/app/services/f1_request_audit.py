"""Metadata-only HTTP attempt audit; no headers, bodies, signed URLs or keys."""
import re
import json
import time
import uuid
import httpx
from app.services import db, execution_store

_installed = False


def install():
    global _installed
    if _installed:
        return
    original = httpx.Client.send

    def send(client, request, *args, **kwargs):
        execution_id = execution_store.current_execution_id()
        identifier = uuid.uuid4().hex
        started = time.monotonic()
        path = re.sub(r'/videos/[^/]+', '/videos/{id}', request.url.path)
        model = None
        try:
            from app.services.f1_controls import MODELS
            candidate = json.loads(request.content).get('model')
            model = candidate if candidate in MODELS else None
        except (ValueError, TypeError, AttributeError, httpx.RequestNotRead):
            pass
        with db.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS f1_http_attempts (request_id TEXT PRIMARY KEY, execution_id TEXT, "
                         "method TEXT NOT NULL, host TEXT NOT NULL, path TEXT NOT NULL, created_at REAL NOT NULL, "
                         "status INTEGER, error_type TEXT, elapsed_ms INTEGER)")
            conn.execute("INSERT INTO f1_http_attempts VALUES(?,?,?,?,?,?,NULL,NULL,NULL)",
                         (identifier, execution_id, request.method, request.url.host, path, time.time()))
            conn.execute("CREATE TABLE IF NOT EXISTS f1_http_response_metadata (request_id TEXT PRIMARY KEY, "
                         "model TEXT, provider_trace TEXT, response_id TEXT)")
            conn.execute('INSERT INTO f1_http_response_metadata VALUES(?,?,NULL,NULL)', (identifier, model))
        status = None
        error_type = None
        trace = None
        response_id = None
        def safe_id(value):
            return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', value) else None
        try:
            response = original(client, request, *args, **kwargs)
            status = response.status_code
            trace = safe_id(response.headers.get('x-request-id') or response.headers.get('x-trace-id') or response.headers.get('tid'))
            if response.is_stream_consumed and 'json' in response.headers.get('content-type', ''):
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        trace = trace or safe_id(body.get('tid') or body.get('request_id'))
                        response_id = safe_id(body.get('id'))
                except ValueError:
                    pass
            return response
        except Exception as error:
            error_type = type(error).__name__
            raise
        finally:
            with db.connect() as conn:
                conn.execute("UPDATE f1_http_attempts SET status=?,error_type=?,elapsed_ms=? WHERE request_id=?",
                             (status, error_type, round((time.monotonic()-started)*1000), identifier))
                conn.execute('UPDATE f1_http_response_metadata SET provider_trace=?,response_id=? WHERE request_id=?',
                             (trace, response_id, identifier))

    httpx.Client.send = send
    _installed = True
