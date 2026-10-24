import json
from unittest.mock import patch

import pytest

from tests.conftest import make_api_key
from tests.test_mcp import rpc

pytestmark = pytest.mark.django_db


def test_served_openapi_has_required_create_contracts(client):
    response = client.get('/api/v1/openapi.json')
    assert response.status_code == 200
    spec = response.json()
    assert spec['openapi'].startswith('3.1')
    schemas = spec['components']['schemas']
    assert {'slug', 'internalName', 'publicTitle', 'formVersion'} <= set(schemas['EventInput']['required'])
    assert {'timezone', 'localStart'} <= set(schemas['OccurrenceInput']['required'])
    assert '/mcp' in spec['paths']


@pytest.mark.parametrize('limit', [-1, 201])
def test_mcp_rejects_out_of_range_limit(client, limit):
    key = make_api_key(('public_read',))
    response = rpc(client, key, 'tools/call', {'name': 'list_events', 'arguments': {'limit': limit}})
    assert response.status_code == 200
    assert response.json()['result']['isError'] is True


def test_mcp_exception_is_not_exposed(client):
    key = make_api_key(('public_read',))
    with patch('webinars.mcp._call', side_effect=RuntimeError('private credential')):
        response = rpc(client, key, 'tools/call', {'name': 'list_events', 'arguments': {}})
    assert response.json()['result']['isError'] is True
    assert 'private credential' not in response.content.decode()


def test_mcp_client_response_accepted(client):
    key = make_api_key(('public_read',))
    response = rpc(client, key, '', raw=json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}))
    assert response.status_code == 202


def test_mcp_key_bucket_enforced(client):
    key = make_api_key(('public_read',))
    with patch('webinars.mcp.ratelimit.hit_key', return_value=False) as hit:
        response = rpc(client, key, 'tools/list')
    assert response.status_code == 429
    assert hit.call_args.args[0] == 'mcp_key'
