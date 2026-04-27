# tests/test_voice_tools.py
import json
import os
import sys
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

_BASE = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(_BASE, 'shared'))
sys.path.insert(0, os.path.join(_BASE, 'lambdas', 'firmos-voice-tools'))


def _event(method, path, query='', body=None):
    return {
        'httpMethod': method,
        'path': path,
        'rawPath': path,
        'rawQueryString': query,
        'headers': {'x-voice-secret': 'test-secret'},
        'body': json.dumps(body) if body else None,
    }


def _mock_conn(fetchone_return=None, fetchall_return=None):
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.return_value = fetchone_return
    mock_cursor.fetchall.return_value = fetchall_return or []
    conn = MagicMock()
    conn.cursor.return_value = mock_cursor
    return conn


def test_unauthorized_returns_401():
    with patch('lambda_function._verify_secret', return_value=False):
        from lambda_function import lambda_handler
        result = lambda_handler(_event('GET', '/firmos/voice/caller', 'phone=%2B1234&org_id=abc'), None)
    assert result['statusCode'] == 401


def test_lookup_caller_new_client():
    with patch('lambda_function._verify_secret', return_value=True), \
         patch('lambda_function._validate_org', return_value=True), \
         patch('lambda_function.get_connection') as mock_get_conn:
        mock_get_conn.return_value = _mock_conn(fetchone_return=None)
        from lambda_function import lambda_handler
        result = lambda_handler(_event('GET', '/firmos/voice/caller', 'phone=%2B12815551234&org_id=abc'), None)
    body = json.loads(result['body'])
    assert result['statusCode'] == 200
    assert body['is_existing_client'] is False


def test_lookup_caller_existing_client():
    contact = {
        'contact_id': 'c1c1c1c1-0001-0001-0001-000000000001',
        'name': 'Maria Garcia',
        'preferred_language': 'es',
    }
    matter = {
        'matter_display_number': 'VEG-001',
        'matter_status': 'open',
        'responsible_attorney_name': 'Ana Vega',
    }
    next_appt = {
        'summary': 'Court Hearing',
        'start_at': datetime(2026, 5, 15, 9, 0, 0, tzinfo=timezone.utc),
    }

    with patch('lambda_function._verify_secret', return_value=True), \
         patch('lambda_function._validate_org', return_value=True), \
         patch('lambda_function.get_connection') as mock_get_conn:
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.side_effect = [contact, matter, next_appt]
        conn = MagicMock()
        conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = conn

        from lambda_function import lambda_handler
        result = lambda_handler(_event('GET', '/firmos/voice/caller', 'phone=%2B12815551234&org_id=abc'), None)

    body = json.loads(result['body'])
    assert result['statusCode'] == 200
    assert body['is_existing_client'] is True
    assert body['name'] == 'Maria Garcia'
    assert 'VEG-001' in body['case_status']
    assert 'Court Hearing' in body['upcoming_appointment']


def test_check_availability_returns_open_slots():
    with patch('lambda_function._verify_secret', return_value=True), \
         patch('lambda_function._validate_org', return_value=True), \
         patch('lambda_function.get_connection') as mock_get_conn:
        mock_get_conn.return_value = _mock_conn(fetchall_return=[])
        from lambda_function import lambda_handler
        result = lambda_handler(_event('GET', '/firmos/voice/availability', 'org_id=abc&date=2026-05-15'), None)
    body = json.loads(result['body'])
    assert result['statusCode'] == 200
    assert len(body['suggested_open']) > 0
    assert body['booked_slots'] == []


def test_validate_org_rejects_unknown_org():
    with patch('lambda_function._verify_secret', return_value=True), \
         patch('lambda_function._validate_org', return_value=False), \
         patch('lambda_function.get_connection'):
        from lambda_function import lambda_handler
        result = lambda_handler(_event('GET', '/firmos/voice/caller', 'phone=%2B1234&org_id=bad-org'), None)
    assert result['statusCode'] == 403


def test_escalate_transfer_returns_phone():
    org = {'emergency_contact_number': '+19365551234'}
    with patch('lambda_function._verify_secret', return_value=True), \
         patch('lambda_function._validate_org', return_value=True), \
         patch('lambda_function.get_connection') as mock_get_conn, \
         patch('lambda_function.boto3') as mock_boto:
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.side_effect = [org, None]
        conn = MagicMock()
        conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = conn

        from lambda_function import lambda_handler
        result = lambda_handler(_event('POST', '/firmos/voice/escalate', body={
            'org_id': 'abc', 'contact_id': 'ccc', 'reason': 'emergency'
        }), None)

    body = json.loads(result['body'])
    assert result['statusCode'] == 200
    assert body['transfer_to'] == '+19365551234'


def test_complete_intake_creates_new_contact():
    with patch('lambda_function._verify_secret', return_value=True), \
         patch('lambda_function._validate_org', return_value=True), \
         patch('lambda_function.get_connection') as mock_get_conn, \
         patch('lambda_function.boto3'):
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = conn

        from lambda_function import lambda_handler
        result = lambda_handler(_event('POST', '/firmos/voice/intake', body={
            'org_id': 'org1',
            'phone': '+12815551234',
            'name': 'John Doe',
            'issue': 'Contract review',
            'language': 'en',
        }), None)

    body = json.loads(result['body'])
    assert result['statusCode'] == 200
    assert body['success'] is True
    assert body['contact_id'] is not None
    assert body['intake_id'] is not None


def test_book_appointment_writes_db_even_if_clio_fails():
    with patch('lambda_function._verify_secret', return_value=True), \
         patch('lambda_function._validate_org', return_value=True), \
         patch('lambda_function.get_connection') as mock_get_conn, \
         patch('lambda_function.requests.post') as mock_post:
        mock_post.side_effect = Exception("Clio timeout")

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        org = {'clio_access_token': 'token123'}
        mock_cursor.fetchone.side_effect = [org, None]
        conn = MagicMock()
        conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = conn

        from lambda_function import lambda_handler
        result = lambda_handler(_event('POST', '/firmos/voice/appointment', body={
            'org_id': 'org1',
            'contact_id': 'c1',
            'start_at': '2026-05-15T10:00:00Z',
            'end_at': '2026-05-15T11:00:00Z',
        }), None)

    body = json.loads(result['body'])
    assert result['statusCode'] == 200
    assert body['confirmed'] is True
    assert conn.commit.called


def test_route_not_found_returns_404():
    with patch('lambda_function._verify_secret', return_value=True), \
         patch('lambda_function.get_connection'):
        from lambda_function import lambda_handler
        result = lambda_handler(_event('GET', '/firmos/voice/invalid'), None)

    assert result['statusCode'] == 404
