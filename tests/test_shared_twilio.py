import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

from unittest.mock import patch, MagicMock
import pytest

def test_send_sms_calls_twilio_client():
    with patch('shared_twilio.Client') as mock_twilio_cls:
        mock_client = MagicMock()
        mock_twilio_cls.return_value = mock_client
        mock_msg = MagicMock()
        mock_msg.sid = 'SM123'
        mock_client.messages.create.return_value = mock_msg

        from shared_twilio import send_sms
        sid = send_sms(
            from_number='+12815550001',
            to_number='+17135550002',
            body='Hello test',
            subaccount_sid='ACsub123',
            subaccount_token='tksub'
        )
        assert sid == 'SM123'
        mock_client.messages.create.assert_called_once()

def test_send_sms_truncates_at_1600_chars():
    with patch('shared_twilio.Client') as mock_twilio_cls:
        mock_client = MagicMock()
        mock_twilio_cls.return_value = mock_client
        mock_msg = MagicMock()
        mock_msg.sid = 'SM456'
        mock_client.messages.create.return_value = mock_msg

        from shared_twilio import send_sms
        send_sms('+1111', '+2222', 'x' * 2000, 'ACsub', 'tok')
        call_kwargs = mock_client.messages.create.call_args
        assert len(call_kwargs.kwargs['body']) == 1600

def test_validate_twilio_signature_raises_on_invalid():
    from shared_twilio import validate_signature
    with pytest.raises(ValueError):
        validate_signature(
            auth_token='wrong-token',
            signature='bad-sig',
            url='https://example.com/webhook',
            params={}
        )
