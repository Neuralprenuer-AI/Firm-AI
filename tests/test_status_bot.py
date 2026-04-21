# tests/test_status_bot.py
import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'firmos-status-bot'))

from unittest.mock import patch, MagicMock
import pytest

def _event():
    return {
        'org_id': 'org-abc', 'contact_id': 'c-abc',
        'conversation_id': 'cv-abc', 'language': 'en'
    }

def _mock_org():
    return {
        'org_id': 'org-abc', 'status': 'active',
        'clio_access_token': 'tok', 'secret_arn': 'arn:test',
        'twilio_phone_number': '+1281', 'twilio_subaccount_sid': 'ACsub'
    }

def test_status_bot_no_clio_token_sends_fallback():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.boto3') as mock_boto, \
         patch('lambda_function.log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        org = dict(_mock_org())
        org['clio_access_token'] = None
        cur.fetchone.side_effect = [org, {'phone': '+17135550001', 'clio_contact_id': None}]
        mock_conn.cursor.return_value.__enter__ = lambda s: cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_lambda = MagicMock()
        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {'SecretString': '{"twilio_auth_token":"tok"}'}
        def side(service, **kw):
            if service == 'lambda': return mock_lambda
            return mock_secrets
        mock_boto.client.side_effect = side
        mock_lambda.invoke.return_value = {'StatusCode': 200}

        from lambda_function import lambda_handler
        lambda_handler(_event(), {})
        mock_lambda.invoke.assert_called_once()

def test_status_bot_clio_returns_matters():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.requests') as mock_req, \
         patch('lambda_function.boto3') as mock_boto, \
         patch('lambda_function.log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.fetchone.side_effect = [
            _mock_org(),
            {'phone': '+17135550001', 'clio_contact_id': '999'}
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'data': [{'id': '1', 'display_number': 'M-001', 'description': 'Visa filing', 'status': 'open', 'practice_area': {'name': 'Immigration'}}]}
        mock_req.get.return_value = mock_resp
        mock_lambda = MagicMock()
        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {'SecretString': '{"twilio_auth_token":"tok"}'}
        def side(service, **kw):
            if service == 'lambda': return mock_lambda
            return mock_secrets
        mock_boto.client.side_effect = side
        mock_lambda.invoke.return_value = {'StatusCode': 200}

        from lambda_function import lambda_handler
        lambda_handler(_event(), {})
        call_args = mock_lambda.invoke.call_args
        payload = json.loads(call_args[1]['Payload'])
        assert 'Immigration' in payload['body']
