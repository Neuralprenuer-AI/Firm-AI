# tests/test_voice_twilio_webhook.py
import json
import os
import urllib.parse
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'firmos-voice-twilio-webhook'))


def _make_twilio_event(from_num='+12815551234', to_num='+19365158417'):
    params = {'From': from_num, 'To': to_num, 'CallSid': 'CA123', 'Direction': 'inbound'}
    body = urllib.parse.urlencode(params)
    return {
        'httpMethod': 'POST',
        'path': '/firmos/voice/call',
        'headers': {'X-Twilio-Signature': 'test-sig'},
        'body': body,
        'isBase64Encoded': False,
    }


def test_returns_twiml_on_success():
    fake_twiml = '<?xml version="1.0"?><Response><Connect><Stream url="wss://elevenlabs"/></Connect></Response>'
    with patch('lambda_function.get_connection') as mock_conn, \
         patch('lambda_function._get_secret') as mock_secret, \
         patch('lambda_function._verify_twilio_signature', return_value=True), \
         patch('lambda_function.requests.post') as mock_post:

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = {
            'org_id': 'a1b2c3d4-0002-0002-0002-000000000002',
            'name': 'Vega Immigration Law',
            'elevenlabs_agent_id': 'agent_abc123',
            'secret_arn': 'arn:aws:secretsmanager:us-east-2:123:secret:test',
        }
        mock_conn.return_value.cursor.return_value = mock_cursor

        mock_secret.return_value = {'twilio_auth_token': 'token123', 'api_key': 'el_key'}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = fake_twiml
        mock_post.return_value = mock_resp

        from lambda_function import lambda_handler
        result = lambda_handler(_make_twilio_event(), None)

    assert result['statusCode'] == 200
    assert result['headers']['Content-Type'] == 'application/xml'
    assert '<Response>' in result['body']


def test_returns_error_twiml_when_org_not_found():
    with patch('lambda_function.get_connection') as mock_conn, \
         patch('lambda_function._verify_twilio_signature', return_value=True):

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None
        mock_conn.return_value.cursor.return_value = mock_cursor

        from lambda_function import lambda_handler
        result = lambda_handler(_make_twilio_event(), None)

    assert result['statusCode'] == 200
    assert 'technical difficulties' in result['body']


def test_rejects_invalid_twilio_signature():
    with patch('lambda_function.get_connection') as mock_conn, \
         patch('lambda_function._get_secret', return_value={'twilio_auth_token': 'real_token'}), \
         patch('lambda_function._verify_twilio_signature', return_value=False):

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = {
            'org_id': 'a1b2c3d4-0002-0002-0002-000000000002',
            'elevenlabs_agent_id': 'agent_abc',
            'secret_arn': 'arn:test',
        }
        mock_conn.return_value.cursor.return_value = mock_cursor

        from lambda_function import lambda_handler
        result = lambda_handler(_make_twilio_event(), None)

    assert result['statusCode'] == 403
