import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'firmos-intake-agent'))

from unittest.mock import patch, MagicMock
import pytest

def _event():
    return {
        'org_id': 'org-abc',
        'contact_id': 'c-abc',
        'conversation_id': 'cv-abc',
        'language': 'en',
        'message': 'My name is John Doe'
    }

def _mock_org():
    return {
        'org_id': 'org-abc',
        'practice_area': 'personal_injury',
        'intake_extra': {},
        'twilio_phone_number': '+12815550002',
        'twilio_subaccount_sid': 'ACsub',
        'secret_arn': 'arn:test'
    }

def test_intake_agent_calls_gemini_and_sends_sms():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.load_prompt_from_s3', return_value='System prompt here'), \
         patch('lambda_function.call_gemini', return_value='What is your name?'), \
         patch('lambda_function.boto3') as mock_boto, \
         patch('lambda_function.log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.fetchone.side_effect = [_mock_org(), {'phone': '+12815550001'}]
        cur.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = lambda s: cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_lambda = MagicMock()
        mock_boto.client.return_value = mock_lambda
        mock_lambda.invoke.return_value = {'StatusCode': 200}
        mock_lambda.get_secret_value.return_value = {'SecretString': '{"twilio_auth_token":"tok"}'}

        from lambda_function import lambda_handler
        lambda_handler(_event(), {})
        mock_lambda.invoke.assert_called()

def test_intake_complete_triggers_clio_sync():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.load_prompt_from_s3', return_value='System prompt'), \
         patch('lambda_function.call_gemini', return_value='INTAKE_COMPLETE'), \
         patch('lambda_function.boto3') as mock_boto, \
         patch('lambda_function.log_audit'):
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.fetchone.side_effect = [_mock_org(), {'phone': '+12815550001'}, {'intake_id': 'i-001'}]
        cur.fetchall.return_value = [
            {'direction': 'inbound', 'body': 'John Doe'},
            {'direction': 'outbound', 'body': 'What happened?'}
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_lambda = MagicMock()
        mock_boto.client.return_value = mock_lambda
        mock_lambda.invoke.return_value = {'StatusCode': 200}
        mock_lambda.get_secret_value.return_value = {'SecretString': '{"twilio_auth_token":"tok"}'}

        from lambda_function import lambda_handler
        lambda_handler(_event(), {})
        calls = [c[1]['FunctionName'] for c in mock_lambda.invoke.call_args_list]
        assert 'firmos-clio-sync' in calls
