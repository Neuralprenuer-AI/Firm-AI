import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

from unittest.mock import patch, MagicMock
import pytest

def test_get_connection_calls_secrets_manager():
    mock_secret = '{"url": "postgresql://user:pass@host:5432/db"}'
    mock_psycopg2 = MagicMock()
    sys.modules['psycopg2'] = mock_psycopg2
    sys.modules['psycopg2.extras'] = MagicMock()

    with patch('boto3.client') as mock_boto:
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.get_secret_value.return_value = {'SecretString': mock_secret}
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_psycopg2.connect.return_value = mock_conn

        import importlib
        import shared_db
        importlib.reload(shared_db)
        shared_db._conn = None
        conn = shared_db.get_connection()

        mock_client.get_secret_value.assert_called_once_with(SecretId='firmos/rds/credentials')
        assert conn is not None

def test_assert_org_access_passes_matching_ids():
    mock_psycopg2 = MagicMock()
    sys.modules['psycopg2'] = mock_psycopg2
    sys.modules['psycopg2.extras'] = MagicMock()

    from shared_db import assert_org_access
    assert_org_access('abc-123', 'abc-123')

def test_assert_org_access_raises_on_mismatch():
    mock_psycopg2 = MagicMock()
    sys.modules['psycopg2'] = mock_psycopg2
    sys.modules['psycopg2.extras'] = MagicMock()

    from shared_db import assert_org_access
    with pytest.raises(PermissionError):
        assert_org_access('abc-123', 'xyz-999')
