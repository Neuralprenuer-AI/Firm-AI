import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

from unittest.mock import patch, MagicMock
import pytest
from conftest import load_lambda

def _event():
    return {
        'org_id': 'org-abc',
        'contact_id': 'c-abc',
        'intake_id': 'i-abc',
        'conversation_text': 'Client: John Doe\nAssistant: What happened?'
    }

def test_clio_sync_creates_note():
    lf = load_lambda('firmos-clio-sync')
    with patch.object(lf, 'get_connection') as mock_conn_fn, \
         patch.object(lf, 'requests') as mock_requests, \
         patch.object(lf, 'log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.fetchone.side_effect = [
            {'org_id': 'org-abc', 'clio_access_token': 'tok-clio',
             'practice_area': 'immigration'},
            {'contact_id': 'c-abc', 'phone': '+17135550001', 'name': 'John',
             'clio_contact_id': 'clio-contact-1'},
            {'intake_id': 'i-abc', 'data': {}, 'clio_note_id': None}
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {'data': {'id': 'note-123'}}
        mock_requests.post.return_value = mock_resp

        lf.lambda_handler(_event(), {})
        mock_requests.post.assert_called_once()

def test_clio_sync_idempotent():
    lf = load_lambda('firmos-clio-sync')
    with patch.object(lf, 'get_connection') as mock_conn_fn, \
         patch.object(lf, 'requests') as mock_requests, \
         patch.object(lf, 'log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.fetchone.side_effect = [
            {'org_id': 'org-abc', 'clio_access_token': 'tok-clio',
             'practice_area': 'immigration'},
            {'contact_id': 'c-abc', 'phone': '+17135550001', 'name': 'John',
             'clio_contact_id': 'clio-contact-1'},
            {'intake_id': 'i-abc', 'data': {}, 'clio_note_id': 'existing-note'}
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        lf.lambda_handler(_event(), {})
        mock_requests.post.assert_not_called()
