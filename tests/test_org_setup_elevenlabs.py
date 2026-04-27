"""
Tests for Step 7: ElevenLabs ConvAI agent provisioning in firmos-org-setup.
"""
import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub out Lambda-layer imports that won't be available during unit tests
# ---------------------------------------------------------------------------
shared_db_stub = types.ModuleType('shared_db')
shared_db_stub.get_connection = MagicMock()
shared_db_stub.log_audit = MagicMock()
sys.modules.setdefault('shared_db', shared_db_stub)

# Now import the function under test
sys.path.insert(0, '/Users/estradas/Claude-Hub/neuralprenuer/active/lawyer-os/Firm-AI/lambdas/firmos-org-setup')
from lambda_function import _provision_elevenlabs_agent  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------
_ORG_ID = 'test-org-uuid-1234'
_PHONE = '+18323334444'
_SUB_SID = 'AC_test_sid'
_SUB_TOKEN = 'test_token'

_EVENT = {
    'firm_name': 'Vega Law',
    'practice_area': 'immigration',
    'agent_display_name': 'Alex',
    'timezone': 'America/Chicago',
}

_EL_SECRET = json.dumps({'api_key': 'el_test_key'})
_WEBHOOK_SECRET = json.dumps({'secret': 'wh_test_secret'})


def _make_sm_client(api_key_secret=_EL_SECRET, webhook_secret=_WEBHOOK_SECRET):
    """Return a mock SM client that returns the given secrets by SecretId."""
    sm = MagicMock()

    def get_secret_value(SecretId):
        if SecretId == 'firmos/elevenlabs/api-key':
            return {'SecretString': api_key_secret}
        if SecretId == 'firmos/voice/webhook-secret':
            return {'SecretString': webhook_secret}
        raise Exception(f'Unknown secret: {SecretId}')

    sm.get_secret_value.side_effect = get_secret_value
    return sm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProvisionElevenLabsAgentSuccess(unittest.TestCase):
    def test_provision_elevenlabs_agent_success(self):
        sm_client = _make_sm_client()

        create_response = MagicMock()
        create_response.status_code = 201
        create_response.json.return_value = {'agent_id': 'el_abc123'}

        phone_response = MagicMock()
        phone_response.status_code = 201

        with patch('lambda_function.requests.post', side_effect=[create_response, phone_response]) as mock_post:
            result = _provision_elevenlabs_agent(
                sm_client, _ORG_ID, _EVENT, _PHONE, _SUB_SID, _SUB_TOKEN
            )

        self.assertEqual(result, 'el_abc123')
        # Verify two POST calls were made: agent create + phone import
        self.assertEqual(mock_post.call_count, 2)


class TestProvisionElevenLabsAgentMissingApiKey(unittest.TestCase):
    def test_provision_elevenlabs_agent_missing_api_key(self):
        sm_client = MagicMock()
        sm_client.get_secret_value.side_effect = Exception('secret not found')

        result = _provision_elevenlabs_agent(
            sm_client, _ORG_ID, _EVENT, _PHONE, _SUB_SID, _SUB_TOKEN
        )

        self.assertIsNone(result)


class TestProvisionElevenLabsAgentApiError(unittest.TestCase):
    def test_provision_elevenlabs_agent_api_error(self):
        sm_client = _make_sm_client()

        error_response = MagicMock()
        error_response.status_code = 500
        error_response.text = 'Internal Server Error'

        with patch('lambda_function.requests.post', return_value=error_response):
            result = _provision_elevenlabs_agent(
                sm_client, _ORG_ID, _EVENT, _PHONE, _SUB_SID, _SUB_TOKEN
            )

        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
