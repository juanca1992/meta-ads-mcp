import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from meta_ads_mcp.core.ads import upload_ad_video
from meta_ads_mcp.core.api import make_api_request
from meta_ads_mcp.core.server import mcp_server


VIDEO = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 24


async def call(**kwargs):
    # Exercise the handler without the legacy decorator's error-wrapping behavior.
    return json.loads(await upload_ad_video.__wrapped__(access_token='test-token', **kwargs))


@pytest.mark.asyncio
async def test_local_upload_streams_file_and_closes_it(tmp_path, monkeypatch):
    path = tmp_path / 'original.mp4'
    path.write_bytes(VIDEO)
    monkeypatch.setenv('META_ADS_VIDEO_UPLOAD_ROOTS', str(tmp_path))
    captured = []

    async def upload(endpoint, token, params, **options):
        assert endpoint == 'act_123/advideos'
        assert token == 'test-token'
        assert params == {'title': 'original.mp4'}
        assert options['method'] == 'POST'
        assert options['timeout'] == 300
        stream = options['files']['source'][1]
        assert stream.read() == VIDEO
        captured.append(stream)
        return {'id': '987'}

    with patch('meta_ads_mcp.core.ads.make_api_request', side_effect=upload) as api:
        result = await call(account_id='123', file_path=str(path))
    assert result['video_id'] == '987'
    assert result['processing_status'] == 'not_checked'
    assert result['bytes'] == len(VIDEO)
    assert captured[0].closed
    api.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', ['', 'data:video/mp4;base64,'])
async def test_base64_upload(prefix):
    with patch('meta_ads_mcp.core.ads.make_api_request', new_callable=AsyncMock, return_value={'id': '77'}) as api:
        result = await call(account_id='act_123', file=prefix + base64.b64encode(VIDEO).decode(), name='My video')
    assert result['success'] is True
    assert result['name'] == 'My video'
    assert api.call_args.kwargs['files']['source'][1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [
    {}, {'file': 'invalid!'}, {'file': base64.b64encode(b'not video').decode()},
    {'file': 'abc', 'file_path': '/tmp/video.mp4'}, {'file_path': 'relative.mp4'},
])
async def test_invalid_input_does_not_contact_meta(kwargs):
    with patch('meta_ads_mcp.core.ads.make_api_request', new_callable=AsyncMock) as api:
        result = await call(account_id='123', **kwargs)
    assert 'error' in result
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_upload_rejects_unconfigured_roots_and_symlink_escape(tmp_path, monkeypatch):
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    private = tmp_path / 'private.mp4'
    private.write_bytes(VIDEO)
    link = allowed / 'link.mp4'
    link.symlink_to(private)
    with patch('meta_ads_mcp.core.ads.make_api_request', new_callable=AsyncMock) as api:
        monkeypatch.delenv('META_ADS_VIDEO_UPLOAD_ROOTS', raising=False)
        assert 'error' in await call(account_id='123', file_path=str(private))
        monkeypatch.setenv('META_ADS_VIDEO_UPLOAD_ROOTS', str(allowed))
        assert 'error' in await call(account_id='123', file_path=str(link))
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_size_limit_before_network(tmp_path, monkeypatch):
    path = tmp_path / 'large.mp4'
    with path.open('wb') as file:
        file.truncate(100 * 1024 * 1024 + 1)
    monkeypatch.setenv('META_ADS_VIDEO_UPLOAD_ROOTS', str(tmp_path))
    with patch('meta_ads_mcp.core.ads.make_api_request', new_callable=AsyncMock) as api:
        assert 'error' in await call(account_id='123', file_path=str(path))
    api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [{'error': {'message': 'Denied', 'code': 200}}, {}])
async def test_unconfirmed_upload_is_not_retried(response):
    with patch('meta_ads_mcp.core.ads.make_api_request', new_callable=AsyncMock, return_value=response) as api:
        result = await call(account_id='123', file=base64.b64encode(VIDEO).decode())
    assert 'error' in result
    assert 'success' not in result
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_discovery():
    tools = await mcp_server.list_tools()
    tool = next(t for t in tools if t.name == 'upload_ad_video')
    assert {'account_id', 'file_path', 'file', 'name'} <= tool.inputSchema['properties'].keys()


@pytest.mark.asyncio
async def test_upload_timeout_not_retried_or_leaked(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('Timeout secret-token', request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr('meta_ads_mcp.core.api.httpx.AsyncClient', lambda: original_client(transport=httpx.MockTransport(handler)))
    result = await make_api_request('act_123/advideos', 'secret-token', {'title': 'example'}, method='POST', files={'source': ('v.mp4', VIDEO, 'video/mp4')})
    assert 'error' in result
    assert 'secret-token' not in json.dumps(result)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_api_multipart_transport_redacts_response_and_preserves_params(monkeypatch):
    def handler(request):
        assert request.method == 'POST'
        assert request.url.path.endswith('/act_123/advideos')
        assert 'multipart/form-data' in request.headers['content-type']
        body = request.read()
        assert b'name="source"' in body and VIDEO in body
        assert b'name="title"' in body and b'example' in body
        return httpx.Response(200, json={'id': '55', 'access_token': 'secret-token'})

    original_client = httpx.AsyncClient
    monkeypatch.setattr('meta_ads_mcp.core.api.httpx.AsyncClient', lambda: original_client(transport=httpx.MockTransport(handler)))
    params = {'title': 'example'}
    result = await make_api_request('act_123/advideos', 'secret-token', params, method='POST', files={'source': ('v.mp4', VIDEO, 'video/mp4')}, timeout=300)
    assert result == {'id': '55', 'access_token': 'REDACTED'}
    assert params == {'title': 'example'}
