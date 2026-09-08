"""Regression coverage for the September audit; all data and transports synthetic."""
import asyncio
import io
import json
import logging
import socket
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpcore
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from meta_ads_mcp.core import ads, api, auth, callback_server, openai_deep_research as research
from meta_ads_mcp.core import server as server_module
from meta_ads_mcp.core.http_auth_integration import AuthInjectionMiddleware, setup_fastmcp_http_auth
from meta_ads_mcp.core.security import open_upload_file
from meta_ads_mcp.core import utils


def app_for(handler=None):
    async def default(request):
        return JSONResponse({'ok': True})
    app = Starlette(routes=[Route('/mcp', handler or default, methods=['POST'])])
    app.add_middleware(AuthInjectionMiddleware)
    return app


HEADERS = {'Authorization': 'Bearer arbitrary-token'}


@pytest.mark.parametrize('body', [[], None, 42, {'method':'tools/call','params':[]}, {'method':'tools/call','params':{'name':[]}}])
def test_invalid_json_rpc_body_is_400(body):
    with TestClient(app_for()) as client:
        response = client.post('/mcp', headers=HEADERS, content=json.dumps(body))
    assert response.status_code == 400


def test_malformed_json_is_400():
    with TestClient(app_for()) as client:
        assert client.post('/mcp', headers=HEADERS, content='{').status_code == 400


@pytest.mark.asyncio
async def test_body_limit_checks_chunked_bytes_before_handler(monkeypatch):
    monkeypatch.setenv('META_ADS_MAX_BODY_BYTES', '1024')
    called = AsyncMock()
    async def handler(request):
        await called()
        return JSONResponse({})
    async def chunks():
        yield b'x' * 700
        yield b'x' * 700
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(handler)), base_url='http://test') as client:
        response = await client.post('/mcp', headers=HEADERS, content=chunks())
    assert response.status_code == 413
    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_request_concurrency_is_bounded():
    release = asyncio.Event()
    entered = asyncio.Event()
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await release.wait()
        return JSONResponse({})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(handler)), base_url='http://test') as client:
        first = asyncio.create_task(client.post('/mcp', headers=HEADERS, json={'method':'tools/list'}))
        second = asyncio.create_task(client.post('/mcp', headers=HEADERS, json={'method':'tools/list'}))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            assert (await client.post('/mcp', headers=HEADERS, json={})).status_code == 429
        finally:
            release.set()
            await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_fetch_isolated_expiring_and_permission_checked(monkeypatch):
    manager = research.MetaAdsDataManager()
    manager._store('owner', 'account:act_123', {'id':'account:act_123','type':'account','text':'private'})
    monkeypatch.setattr(research, '_data_manager', manager)
    with patch.object(research, 'make_api_request', AsyncMock(return_value={'id':'act_123'})) as graph:
        assert 'private' not in await research.fetch(id='account:act_123', access_token='attacker')
        graph.assert_not_awaited()
        assert 'private' in await research.fetch(id='account:act_123', access_token='owner')
        graph.return_value = {'error': {'code':190}}
        assert 'private' not in await research.fetch(id='account:act_123', access_token='owner')
    with patch.object(research.time, 'monotonic', return_value=10**20):
        assert manager.fetch_record('account:act_123', 'owner') is None
    manager.cache_limit = 2
    for i in range(3):
        manager._store('owner', str(i), {'type':'account'})
    assert len(manager._cache) == 2


def test_http_local_upload_blocked_before_file_open(tmp_path, monkeypatch):
    monkeypatch.setenv('META_ADS_VIDEO_UPLOAD_ROOTS', str(tmp_path))
    async def handler(request):
        result = await ads.upload_ad_video(account_id='999', file_path=str(tmp_path/'fixture.mp4'), access_token='other-user')
        return JSONResponse(json.loads(result))
    with patch('meta_ads_mcp.core.security.open_upload_file') as read:
        with TestClient(app_for(handler)) as client:
            response = client.post('/mcp', headers=dict(HEADERS, **{'X-META-WRITE-CONFIRMATION':'upload_ad_video'}), json={'method':'tools/call','params':{'name':'upload_ad_video'}})
    assert 'disabled over HTTP' in response.text
    read.assert_not_called()


def test_secure_file_walk_rejects_directory_symlink_and_fifo(tmp_path, monkeypatch):
    monkeypatch.setenv('META_ADS_VIDEO_UPLOAD_ROOTS', str(tmp_path))
    (tmp_path/'dir').mkdir()
    (tmp_path/'dir'/'a.mp4').write_bytes(b'video')
    (tmp_path/'link').symlink_to(tmp_path/'dir', target_is_directory=True)
    with pytest.raises(OSError):
        open_upload_file(str(tmp_path/'link'/'a.mp4'))
    import os
    os.mkfifo(tmp_path/'fifo.mp4')
    with pytest.raises(ValueError):
        open_upload_file(str(tmp_path/'fifo.mp4'))


@pytest.mark.asyncio
async def test_httpx_logs_have_no_token_or_proof(monkeypatch, caplog):
    monkeypatch.setenv('META_APP_SECRET', 'synthetic-app-secret')
    original = httpx.AsyncClient
    def handler(request):
        assert 'access_token' not in request.url.params
        assert request.headers['authorization'] == 'Bearer synthetic-access-token'
        return httpx.Response(200, json={'id':'123'})
    with patch.object(api.httpx, 'AsyncClient', lambda: original(transport=httpx.MockTransport(handler))):
        with caplog.at_level(logging.DEBUG):
            await api.make_api_request('me', 'synthetic-access-token')
    assert 'synthetic-access-token' not in caplog.text
    assert 'synthetic-app-secret' not in caplog.text
    import hashlib, hmac
    proof = hmac.new(b'synthetic-app-secret', b'synthetic-access-token', hashlib.sha256).hexdigest()
    assert proof not in caplog.text


@pytest.mark.asyncio
async def test_image_upload_does_not_write_stdout(capsys):
    with patch.object(ads, 'make_api_request', AsyncMock(return_value={'images':{'h':{'hash':'h'}}})):
        await ads.upload_ad_image(account_id='123', access_token='dummy', file='QUJD')
    assert capsys.readouterr().out == ''


def callback(path):
    handler = object.__new__(callback_server.CallbackHandler)
    handler.path = path
    handler.wfile = io.BytesIO()
    status = []
    handler.send_response = status.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    return handler, status


def test_oauth_state_is_one_use_and_callback_never_reflects_input():
    manager = object.__new__(auth.AuthManager)
    manager.app_id = 'app'
    manager.redirect_uri = 'http://localhost:8080/callback'
    params = parse_qs(urlparse(manager.get_auth_url()).query)
    assert params['response_type'] == ['code']
    state = params['state'][0]
    handler, status = callback('/callback?error=%3Cscript%3E&state=wrong')
    handler.do_GET()
    assert status == [400] and b'<script>' not in handler.wfile.getvalue()
    handler, status = callback('/callback?code=fake-code&state='+state)
    with patch.object(auth, 'exchange_authorization_code', return_value={'token':'fake','expires_in':3600}) as exchange:
        with patch.object(auth, 'process_token_response', return_value=True):
            handler.do_GET()
    assert status == [200]
    exchange.assert_called_once_with('fake-code', manager.redirect_uri)
    handler, status = callback('/callback?code=fake-code&state='+state)
    handler.do_GET()
    assert status == [400]
    handler, status = callback('/token')
    handler.do_GET()
    assert status == [404] and b'fake' not in handler.wfile.getvalue()


def test_oauth_expired_state_rejected():
    with patch.object(callback_server.time, 'monotonic', return_value=0):
        state = callback_server.begin_oauth_flow('http://localhost/callback')
    with patch.object(callback_server.time, 'monotonic', return_value=1000):
        assert callback_server.consume_oauth_state(state) is None


def test_code_exchange_timeout_and_payload(monkeypatch):
    monkeypatch.setenv('META_APP_SECRET','secret')
    response = httpx.Response(200, json={'access_token':'short','expires_in':3600}, request=httpx.Request('GET','https://graph.facebook.com'))
    with patch.object(auth.requests, 'get', return_value=response) as request:
        assert auth.exchange_authorization_code('code','http://localhost/callback')['token'] == 'short'
    assert request.call_args.kwargs['timeout'] == 30
    assert request.call_args.kwargs['params']['redirect_uri'] == 'http://localhost/callback'


def test_server_does_not_run_after_auth_setup_failure(monkeypatch):
    monkeypatch.setattr('sys.argv', ['meta-ads-mcp','--transport','streamable-http'])
    with patch('meta_ads_mcp.core.http_auth_integration.setup_fastmcp_http_auth', side_effect=RuntimeError('synthetic failure')):
        with patch.object(server_module.mcp_server, 'run') as run:
            assert server_module.main() == 1
    run.assert_not_called()


def test_missing_app_provider_fails_closed():
    with pytest.raises(RuntimeError):
        setup_fastmcp_http_auth(object())


@pytest.mark.asyncio
async def test_dns_pinning_and_rebinding_block(monkeypatch):
    from meta_ads_mcp.core.download_transport import PublicNetworkBackend
    import anyio
    backend = PublicNetworkBackend()
    monkeypatch.setattr(anyio, 'getaddrinfo', AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]))
    with patch.object(httpcore.AnyIOBackend, 'connect_tcp', AsyncMock(return_value='stream')) as connect:
        assert await backend.connect_tcp('public.example',443) == 'stream'
        assert connect.call_args.args[0] == '8.8.8.8'
    monkeypatch.setattr(anyio, 'getaddrinfo', AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]))
    with patch.object(httpcore.AnyIOBackend, 'connect_tcp', AsyncMock()) as connect:
        with pytest.raises(utils.BlockedURLError):
            await backend.connect_tcp('public.example',443)
        connect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{'content-length':str(21*1024*1024)}, {'content-encoding':'gzip'}])
async def test_image_download_rejects_oversize_and_compression(headers):
    original = httpx.AsyncClient
    with patch.object(utils.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers=headers, content=b'fake')))):
        assert await utils.download_image('https://8.8.8.8/image') is None


@pytest.mark.asyncio
async def test_chunked_image_enforces_accumulated_limit(monkeypatch):
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x'*700
            yield b'x'*700
    original = httpx.AsyncClient
    monkeypatch.setattr(utils,'MAX_IMAGE_BYTES',1024)
    with patch.object(utils.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(200,stream=Chunks())))):
        assert await utils.download_image('https://8.8.8.8/image') is None


@pytest.mark.asyncio
async def test_http_auth_never_falls_back_to_operator_token(monkeypatch):
    from meta_ads_mcp.core.http_auth_integration import _http_request, FastMCPAuthIntegration
    monkeypatch.setenv('META_ACCESS_TOKEN', 'operator-token-that-must-not-be-used')
    marker = _http_request.set(True)
    try:
        FastMCPAuthIntegration.set_auth_token('request-token')
        assert await auth.get_current_access_token() == 'request-token'
        FastMCPAuthIntegration.clear_auth_token()
        assert await auth.get_current_access_token() is None
        with patch.object(api.auth_manager, 'invalidate_token') as invalidate:
            api._invalidate_local_auth()
            invalidate.assert_not_called()
    finally:
        FastMCPAuthIntegration.clear_auth_token()
        _http_request.reset(marker)


def test_middleware_install_failure_propagates():
    from meta_ads_mcp.core.http_auth_integration import setup_starlette_middleware
    app = Starlette()
    with patch.object(app, 'add_middleware', side_effect=RuntimeError('already started')):
        with pytest.raises(RuntimeError):
            setup_starlette_middleware(app)


@pytest.mark.asyncio
async def test_pagination_keeps_trusted_endpoint_and_no_partial_success():
    request = AsyncMock(side_effect=[
        {'data':[{'id':'1'}], 'paging':{'next':'https://untrusted.example/token','cursors':{'after':'a'}}},
        {'data':[{'id':'2'}]},
    ])
    result = await api.make_paginated_request('me/adaccounts', 'token', {'limit':1}, request=request)
    assert [row['id'] for row in result['data']] == ['1','2']
    assert all(call.args[0] == 'me/adaccounts' for call in request.call_args_list)
    assert request.call_args_list[1].args[2]['after'] == 'a'
    request = AsyncMock(return_value={'data':[], 'paging':{'next':'https://example','cursors':{'after':'a'}}})
    result = await api.make_paginated_request('me/adaccounts', 'token', {}, request=request)
    assert 'error' in result and 'data' not in result


def test_log_redaction_covers_raw_oauth_query_and_mapping(caplog):
    with caplog.at_level(logging.DEBUG):
        logging.getLogger('urllib3.connectionpool').debug('GET /oauth?client_secret=secret123&code=code123&state=state123 HTTP/1.1')
        logging.getLogger('thirdparty').info("Payload %s", {'access_token':'token123'})
    for value in ('secret123','code123','state123','token123'):
        assert value not in caplog.text


def test_real_mcp_fetch_respects_http_identity_and_host(monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    mcp = FastMCP('security-integration')
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=['testserver'], allowed_origins=[])
    mcp.tool()(research.fetch)
    setup_fastmcp_http_auth(mcp)
    manager = research.MetaAdsDataManager()
    manager._store('owner', 'account:act_123', {'id':'account:act_123','type':'account','text':'private-record'})
    monkeypatch.setattr(research, '_data_manager', manager)
    headers = {'Authorization':'Bearer attacker', 'Accept':'application/json, text/event-stream'}
    call = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'fetch','arguments':{'id':'account:act_123'}}}
    with patch.object(research, 'make_api_request', AsyncMock(return_value={'id':'act_123'})):
        with TestClient(mcp.streamable_http_app()) as client:
            response = client.post('/mcp', headers=headers, json=call)
            assert response.status_code == 200 and 'private-record' not in response.text
            response = client.post('/mcp', headers={**headers, 'Authorization':'Bearer owner'}, json=call)
            assert response.status_code == 200 and 'private-record' in response.text
            response = client.post('/mcp', headers={**headers, 'Host':'attacker.example'}, json=call)
            assert response.status_code == 421 and 'private-record' not in response.text


@pytest.mark.asyncio
async def test_public_download_success_with_streaming_transport():
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first'
            yield b'second'
    original = httpx.AsyncClient
    with patch.object(utils.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(200,stream=Chunks())))):
        assert await utils.download_image('https://8.8.8.8/image') == b'firstsecond'


def test_exception_trace_redacts_request_token(caplog):
    from meta_ads_mcp.core.http_auth_integration import FastMCPAuthIntegration
    FastMCPAuthIntegration.set_auth_token('private-request-credential')
    try:
        with caplog.at_level(logging.ERROR):
            try:
                raise ValueError('private-request-credential')
            except ValueError:
                logging.getLogger('thirdparty').exception('request failed')
    finally:
        FastMCPAuthIntegration.clear_auth_token()
    assert 'private-request-credential' not in caplog.text
    assert 'REDACTED' in caplog.text
