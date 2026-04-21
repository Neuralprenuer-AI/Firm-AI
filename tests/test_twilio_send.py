import sys, os
from unittest.mock import patch, MagicMock
import importlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'firmos-twilio-send'))

mock_psycopg2 = MagicMock()
sys.modules['psycopg2'] = mock_psycopg2
sys.modules['psycopg2.extras'] = MagicMock()

mock_twilio = MagicMock()
sys.modules['twilio'] = mock_twilio
sys.modules['twilio.rest'] = MagicMock()
sys.modules['twilio.request_validator'] = MagicMock()

def _mock_org():
    return {
        'org_id': 'org-abc',
        'twilio_phone_number': '+12815550001',
        'twilio_subaccount_sid': 'ACsub',
        'monthly_sms_budget': 500
    }

def test_send_succeeds_under_budget():
    with patch('shared_db.get_connection') as mock_conn_fn, \
         patch('shared_twilio.send_sms') as mock_send, \
         patch('shared_db.log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn

        call_count = [0]
        org_data = _mock_org()
        budget_data = {'count': 10}

        def mock_fetchone():
            call_count[0] += 1
            if call_count[0] == 1:
                return org_data
            return budget_data

        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = mock_fetchone

        mock_conn.cursor.return_value = cur
        mock_send.return_value = 'SM123'

        if 'lambda_function' in sys.modules:
            del sys.modules['lambda_function']
        import lambda_function
        result = lambda_function.lambda_handler({
            'org_id': 'org-abc',
            'to_phone': '+17135550002',
            'body': 'Hello',
            'subaccount_token': 'tksub'
        }, {})
        assert result['success'] is True
        assert result['twilio_message_sid'] == 'SM123'

def test_send_blocked_at_budget():
    with patch('shared_db.get_connection') as mock_conn_fn, \
         patch('shared_db.log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn

        call_count = [0]
        org_data = _mock_org()
        budget_data = {'count': 500}

        def mock_fetchone():
            call_count[0] += 1
            if call_count[0] == 1:
                return org_data
            return budget_data

        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = mock_fetchone

        mock_conn.cursor.return_value = cur

        if 'lambda_function' in sys.modules:
            del sys.modules['lambda_function']
        import lambda_function
        result = lambda_function.lambda_handler({
            'org_id': 'org-abc',
            'to_phone': '+17135550002',
            'body': 'Hello',
            'subaccount_token': 'tksub'
        }, {})
        assert result['success'] is False
        assert result['error'] == 'budget_exceeded'
