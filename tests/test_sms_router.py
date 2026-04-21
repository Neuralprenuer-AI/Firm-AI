# tests/test_sms_router.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'firmos-sms-router'))

from unittest.mock import patch, MagicMock, call
import pytest

def _event(body='Hello'):
    return {'org_id': 'org-abc', 'from_phone': '+17135550001', 'to_phone': '+12815550002', 'body': body}

def _mock_org():
    return {'org_id': 'org-abc', 'status': 'active', 'secret_arn': 'arn:test',
            'twilio_phone_number': '+12815550002', 'twilio_subaccount_sid': 'ACsub'}

def _make_conn(org, contact, conv):
    mock_conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: cur
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.side_effect = [org, contact, conv]
    mock_conn.cursor.return_value = cur
    return mock_conn, cur

def test_new_contact_gets_language_prompt():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.log_audit'), \
         patch('lambda_function.boto3') as mock_boto:
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.side_effect = [
            _mock_org(),
            None,
            {'contact_id': 'c-1', 'phone': '+17135550001', 'preferred_language': 'pending'},
            None,
            {'conversation_id': 'cv-1', 'state': 'language_pending'}
        ]
        mock_conn.cursor.return_value = cur
        mock_lambda = MagicMock()
        mock_secrets = MagicMock()
        mock_boto.client.side_effect = lambda svc, **kw: mock_lambda if svc == 'lambda' else mock_secrets
        mock_secrets.get_secret_value.return_value = {'SecretString': '{"twilio_auth_token":"tok"}'}
        mock_lambda.invoke.return_value = {'StatusCode': 200}

        from lambda_function import lambda_handler
        lambda_handler(_event(), {})
        mock_lambda.invoke.assert_called()

def test_reply_1_sets_english_and_starts_intake():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.log_audit'), \
         patch('lambda_function.boto3') as mock_boto:
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.side_effect = [
            _mock_org(),
            {'contact_id': 'c-1', 'phone': '+17135550001', 'preferred_language': 'pending'},
            {'conversation_id': 'cv-1', 'state': 'language_pending'}
        ]
        mock_conn.cursor.return_value = cur
        mock_lambda = MagicMock()
        mock_secrets = MagicMock()
        mock_boto.client.side_effect = lambda svc, **kw: mock_lambda if svc == 'lambda' else mock_secrets
        mock_secrets.get_secret_value.return_value = {'SecretString': '{"twilio_auth_token":"tok"}'}
        mock_lambda.invoke.return_value = {'StatusCode': 200}

        from lambda_function import lambda_handler
        lambda_handler(_event(body='1'), {})
        invoked = [c[1]['FunctionName'] for c in mock_lambda.invoke.call_args_list]
        assert 'firmos-intake-agent' in invoked

def test_escalation_keyword_triggers_escalation():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.log_audit'), \
         patch('lambda_function.boto3') as mock_boto:
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.side_effect = [
            _mock_org(),
            {'contact_id': 'c-1', 'phone': '+17135550001', 'preferred_language': 'es'},
            {'conversation_id': 'cv-1', 'state': 'intake_in_progress'}
        ]
        mock_conn.cursor.return_value = cur
        mock_lambda = MagicMock()
        mock_secrets = MagicMock()
        mock_boto.client.side_effect = lambda svc, **kw: mock_lambda if svc == 'lambda' else mock_secrets
        mock_lambda.invoke.return_value = {'StatusCode': 200}

        from lambda_function import lambda_handler
        lambda_handler(_event(body='ICE esta afuera'), {})
        invoked = [c[1]['FunctionName'] for c in mock_lambda.invoke.call_args_list]
        assert 'firmos-escalation' in invoked
