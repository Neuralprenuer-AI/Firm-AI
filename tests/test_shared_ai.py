import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

from unittest.mock import patch, MagicMock
import importlib
import pytest

def test_call_gemini_returns_text():
    with patch('boto3.client') as mock_boto, \
         patch('requests.post') as mock_post:
        mock_boto.return_value.get_secret_value.return_value = {
            'SecretString': '{"api_key": "test-key"}'
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'candidates': [{'content': {'parts': [{'text': 'Hello from Gemini'}]}}]
        }
        mock_post.return_value = mock_resp

        import shared_ai
        importlib.reload(shared_ai)
        shared_ai._api_key = None  # reset cached key
        result = shared_ai.call_gemini(system_prompt='You are helpful.', user_message='Hello')
        assert result == 'Hello from Gemini'

def test_call_gemini_truncates_long_response():
    with patch('boto3.client') as mock_boto, \
         patch('requests.post') as mock_post:
        mock_boto.return_value.get_secret_value.return_value = {
            'SecretString': '{"api_key": "test-key"}'
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'candidates': [{'content': {'parts': [{'text': 'x' * 2000}]}}]
        }
        mock_post.return_value = mock_resp

        import shared_ai
        importlib.reload(shared_ai)
        shared_ai._api_key = None
        result = shared_ai.call_gemini(system_prompt='You are helpful.', user_message='Hello', max_chars=1600)
        assert len(result) <= 1600

def test_load_prompt_from_s3():
    with patch('boto3.client') as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3
        mock_body = MagicMock()
        mock_body.read.return_value = b'You are an intake agent.'
        mock_s3.get_object.return_value = {'Body': mock_body}

        import shared_ai
        importlib.reload(shared_ai)
        result = shared_ai.load_prompt_from_s3('immigration')
        mock_s3.get_object.assert_called_once_with(
            Bucket='firmos-documents-006619321854',
            Key='prompts/immigration/intake_v1.txt'
        )
        assert result == 'You are an intake agent.'
