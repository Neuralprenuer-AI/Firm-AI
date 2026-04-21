import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'firmos-sms-webhook'))

from unittest.mock import patch, MagicMock
import urllib.parse
import pytest

def _webhook_event(body='Hello', from_num='+17135550001', to_num='+12815550002'):
    params = {'Body': body, 'From': from_num, 'To': to_num}
    return {
        'headers': {'X-Twilio-Signature': 'sig123'},
        'body': urllib.parse.urlencode(params),
        'requestContext': {'domainName': 'api.example.com', 'path': '/firmos/webhook/sms'}
    }

def _mock_org():
    return {'org_id': 'org-abc', 'twilio_subaccount_sid': 'ACsub', 'secret_arn': 'arn:test'}

def test_webhook_routes_to_router():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.validate_signature'), \
         patch('lambda_function.boto3') as mock_boto:
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = _mock_org()
        mock_conn.cursor.return_value = cur
        mock_secrets = MagicMock()
        mock_lambda = MagicMock()
        mock_boto.client.side_effect = lambda svc, **kw: mock_secrets if svc == 'secretsmanager' else mock_lambda
        mock_secrets.get_secret_value.return_value = {'SecretString': '{"twilio_auth_token":"tok"}'}
        mock_lambda.invoke.return_value = {'StatusCode': 200}

        from lambda_function import lambda_handler
        result = lambda_handler(_webhook_event(), {})
        assert result['statusCode'] == 200
        mock_lambda.invoke.assert_called_once()

def test_webhook_returns_403_on_invalid_signature():
    with patch('lambda_function.get_connection') as mock_conn_fn, \
         patch('lambda_function.validate_signature', side_effect=ValueError("bad sig")), \
         patch('lambda_function.boto3') as mock_boto:
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = _mock_org()
        mock_conn.cursor.return_value = cur
        mock_secrets = MagicMock()
        mock_boto.client.return_value = mock_secrets
        mock_secrets.get_secret_value.return_value = {'SecretString': '{"twilio_auth_token":"tok"}'}

        from lambda_function import lambda_handler
        result = lambda_handler(_webhook_event(), {})
        assert result['statusCode'] == 403

def test_webhook_returns_404_for_unknown_number():
    with patch('lambda_function.get_connection') as mock_conn_fn:
        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = None
        mock_conn.cursor.return_value = cur

        from lambda_function import lambda_handler
        result = lambda_handler(_webhook_event(), {})
        assert result['statusCode'] == 404
