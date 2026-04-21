import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'firmos-escalation'))

from unittest.mock import patch, MagicMock
import pytest

def _event():
    return {
        'org_id': 'org-abc',
        'contact_id': 'c-abc',
        'conversation_id': 'cv-abc',
        'triggered_keyword': 'ICE',
        'message_body': 'ICE is at my door'
    }

def test_escalation_creates_record_and_notifies():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.boto3') as mock_boto, \
         patch('lambda_function.log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.fetchone.side_effect = [
            {'org_id': 'org-abc', 'name': 'Test Firm', 'secret_arn': 'arn:test',
             'twilio_phone_number': '+12815550002', 'twilio_subaccount_sid': 'ACsub'},
            {'phone': '+17135550001', 'name': 'John Doe'},
            {'escalation_id': 'esc-1'}
        ]
        cur.fetchall.return_value = [
            {'user_id': 'u-1', 'email': 'atty@firm.com', 'name': 'Jane Smith',
             'phone': '+17135559999'}
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_ses = MagicMock()
        mock_lambda = MagicMock()
        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {
            'SecretString': '{"twilio_auth_token":"tok","sender":"noreply@test.com"}'
        }

        def client_side_effect(service, **kwargs):
            if service == 'ses': return mock_ses
            if service == 'lambda': return mock_lambda
            if service == 'secretsmanager': return mock_secrets
            return MagicMock()

        mock_boto.client.side_effect = client_side_effect
        mock_lambda.invoke.return_value = {'StatusCode': 200}

        from lambda_function import lambda_handler
        lambda_handler(_event(), {})
        mock_ses.send_email.assert_called_once()
