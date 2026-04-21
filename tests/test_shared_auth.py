# tests/test_shared_auth.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

import jwt, time
from unittest.mock import patch, MagicMock
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import pytest

def _make_rsa_pair():
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    return private_key, private_key.public_key()

def _make_token(private_key, role='firm_admin', org_id='org-abc', exp_offset=3600):
    return jwt.encode(
        {'sub': 'user-uuid-123', 'exp': int(time.time()) + exp_offset,
         'app_metadata': {'role': role, 'org_id': org_id}},
        private_key, algorithm='RS256'
    )

def test_verify_jwt_returns_claims():
    priv, pub = _make_rsa_pair()
    token = _make_token(priv)
    mock_signing_key = MagicMock()
    mock_signing_key.key = pub
    with patch('shared_auth._get_jwks_client') as mock_client_fn:
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = mock_signing_key
        mock_client_fn.return_value = mock_client
        from shared_auth import verify_jwt
        claims = verify_jwt(token)
        assert claims['app_metadata']['role'] == 'firm_admin'

def test_verify_jwt_raises_on_expired():
    priv, pub = _make_rsa_pair()
    token = _make_token(priv, exp_offset=-10)
    mock_signing_key = MagicMock()
    mock_signing_key.key = pub
    with patch('shared_auth._get_jwks_client') as mock_client_fn:
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = mock_signing_key
        mock_client_fn.return_value = mock_client
        from shared_auth import verify_jwt
        with pytest.raises(Exception):
            verify_jwt(token)

def test_get_org_id_from_claims():
    from shared_auth import get_org_id
    claims = {'app_metadata': {'role': 'firm_admin', 'org_id': 'org-xyz'}}
    assert get_org_id(claims) == 'org-xyz'

def test_require_role_passes():
    from shared_auth import require_role
    claims = {'app_metadata': {'role': 'super_admin'}}
    require_role(claims, 'super_admin')

def test_require_role_raises():
    from shared_auth import require_role
    claims = {'app_metadata': {'role': 'firm_admin'}}
    with pytest.raises(PermissionError):
        require_role(claims, 'super_admin')
