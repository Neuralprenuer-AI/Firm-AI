import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

from unittest.mock import patch, MagicMock
import pytest

def test_call_gemini_returns_text():
    with patch('boto3.client') as mock_boto, \
         patch('google.generativeai.configure'), \
         patch('google.generativeai.GenerativeModel') as mock_model_cls:
        mock_boto.return_value.get_secret_value.return_value = {
            'SecretString': '{"api_key": "test-key"}'
        }
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = "Hello from Gemini"
        mock_model.generate_content.return_value = mock_response

        import importlib, shared_ai
        importlib.reload(shared_ai)
        result = shared_ai.call_gemini(system_prompt="You are helpful.", user_message="Hello")
        assert result == "Hello from Gemini"

def test_call_gemini_truncates_long_response():
    with patch('boto3.client') as mock_boto, \
         patch('google.generativeai.configure'), \
         patch('google.generativeai.GenerativeModel') as mock_model_cls:
        mock_boto.return_value.get_secret_value.return_value = {
            'SecretString': '{"api_key": "test-key"}'
        }
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = "x" * 2000
        mock_model.generate_content.return_value = mock_response

        import importlib, shared_ai
        importlib.reload(shared_ai)
        result = shared_ai.call_gemini(system_prompt="You are helpful.", user_message="Hello", max_chars=1600)
        assert len(result) <= 1600

def test_load_prompt_from_s3():
    with patch('boto3.client') as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3
        mock_body = MagicMock()
        mock_body.read.return_value = b'You are an intake agent.'
        mock_s3.get_object.return_value = {'Body': mock_body}

        import importlib, shared_ai
        importlib.reload(shared_ai)
        result = shared_ai.load_prompt_from_s3('immigration')
        mock_s3.get_object.assert_called_once_with(
            Bucket='firmos-documents-006619321854',
            Key='prompts/immigration/intake_v1.txt'
        )
        assert result == 'You are an intake agent.'
