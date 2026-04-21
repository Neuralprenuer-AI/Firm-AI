import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'firmos-crud'))

from unittest.mock import patch, MagicMock
import jwt, time, json, pytest

SECRET = 'test-secret-32-chars-minimum-len!'


def _token(role='firm_admin', org_id='org-abc'):
    return jwt.encode({'sub': 'uid-1', 'exp': int(time.time()) + 3600,
                       'app_metadata': {'role': role, 'org_id': org_id}}, SECRET, algorithm='HS256')


def _event(path='/firmos/firms', method='GET', role='super_admin', org_id=None, body=None):
    return {
        'path': path, 'httpMethod': method,
        'headers': {'Authorization': f'Bearer {_token(role, org_id)}'},
        'pathParameters': None, 'queryStringParameters': None,
        'body': json.dumps(body) if body else None
    }


def test_get_firms_requires_super_admin():
    with patch('lambda_function._get_secret', return_value=SECRET):
        from lambda_function import lambda_handler
        with patch('lambda_function.get_connection') as mock_conn_fn:
            mock_conn = MagicMock()
            mock_conn_fn.return_value = mock_conn
            cur = MagicMock()
            cur.fetchall.return_value = []
            mock_conn.cursor.return_value.__enter__ = lambda s: cur
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            result = lambda_handler(_event('/firmos/firms', 'GET', 'super_admin'), {})
            assert result['statusCode'] == 200


def test_get_firms_blocks_firm_admin():
    with patch('lambda_function._get_secret', return_value=SECRET):
        from lambda_function import lambda_handler
        with patch('lambda_function.get_connection') as mock_conn_fn:
            mock_conn_fn.return_value = MagicMock()
            result = lambda_handler(_event('/firmos/firms', 'GET', 'firm_admin', 'org-abc'), {})
            assert result['statusCode'] == 403
