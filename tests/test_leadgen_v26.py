import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from meta_ads_mcp.core import auth
from meta_ads_mcp.core.api import META_GRAPH_API_VERSION
from meta_ads_mcp.core.leadgen import (
    archive_lead_form,
    create_lead_form,
    get_leadgen_eligibility,
    get_leads,
    subscribe_page_leadgen_webhook,
)
from meta_ads_mcp.core.http_auth_integration import _MUTATING_TOOLS


def test_graph_and_oauth_are_v26_with_write_and_lead_scopes():
    assert META_GRAPH_API_VERSION == "v26.0"
    scopes = set(auth.AUTH_SCOPE.split(","))
    assert {"ads_management", "pages_manage_ads", "pages_manage_metadata", "leads_retrieval"} <= scopes
    assert {
        "create_lead_form", "update_lead_form", "archive_lead_form",
        "create_test_lead", "subscribe_page_leadgen_webhook",
        "unsubscribe_page_leadgen_webhook",
    } <= _MUTATING_TOOLS


@pytest.mark.asyncio
async def test_leadgen_eligibility_reports_required_action():
    with patch("meta_ads_mcp.core.leadgen.make_api_request", AsyncMock(return_value={
        "id": "123", "name": "Page", "leadgen_tos_accepted": False,
    })):
        result = json.loads(await get_leadgen_eligibility("123", access_token="token"))
    assert result["eligible_for_lead_ads"] is False
    assert "action_required" in result


@pytest.mark.asyncio
async def test_create_lead_form_supports_current_quality_and_compliance_fields():
    request = AsyncMock(return_value={"id": "456"})
    with patch("meta_ads_mcp.core.leadgen.make_api_request", request):
        result = json.loads(await create_lead_form(
            page_id="123",
            name="Qualified leads",
            locale="es_la",
            questions=[{"type": "EMAIL"}],
            privacy_policy_url="https://example.com/privacy",
            custom_disclaimer={"title": "Consent"},
            thank_you_page={"title": "Thanks"},
            is_optimized_for_quality=True,
            is_phone_sms_verify_enabled=True,
            should_enforce_work_email=True,
            is_lead_capture_ai_agent_enabled=False,
            access_token="token",
        ))
    assert result == {"id": "456"}
    endpoint, token, params = request.await_args.args[:3]
    assert endpoint == "123/leadgen_forms"
    assert token == "token"
    assert params["locale"] == "ES_LA"
    assert params["privacy_policy"]["url"] == "https://example.com/privacy"
    assert params["is_optimized_for_quality"] is True
    assert request.await_args.kwargs["method"] == "POST"


@pytest.mark.asyncio
async def test_create_lead_form_uploads_gated_pdf_as_multipart():
    request = AsyncMock(return_value={"id": "456"})
    pdf = base64.b64encode(b"%PDF-1.7\nsmall").decode()
    with patch("meta_ads_mcp.core.leadgen.make_api_request", request):
        await create_lead_form(
            "123", "Form", "EN_US", [{"type": "EMAIL"}],
            privacy_policy={"url": "https://example.com/privacy", "link_text": "Privacy"},
            gated_file_base64=pdf,
            access_token="token",
        )
    upload = request.await_args.kwargs["files"]["upload_gated_file"]
    assert upload[0] == "gated-content.pdf"
    assert upload[2] == "application/pdf"


@pytest.mark.asyncio
async def test_get_leads_preserves_pagination_cursor():
    request = AsyncMock(return_value={"data": []})
    with patch("meta_ads_mcp.core.leadgen.make_api_request", request):
        await get_leads("456", limit=40, after="cursor", access_token="token")
    assert request.await_args.args[2]["after"] == "cursor"
    assert request.await_args.args[2]["limit"] == 40


@pytest.mark.asyncio
async def test_webhook_subscription_uses_leadgen_field():
    request = AsyncMock(return_value={"success": True})
    with patch("meta_ads_mcp.core.leadgen.make_api_request", request):
        await subscribe_page_leadgen_webhook("123", access_token="token")
    assert request.await_args.args[0] == "123/subscribed_apps"
    assert request.await_args.args[2] == {"subscribed_fields": ["leadgen"]}


@pytest.mark.asyncio
async def test_archive_lead_form_sets_archived_status():
    request = AsyncMock(return_value={"success": True})
    with patch("meta_ads_mcp.core.leadgen.make_api_request", request):
        result = json.loads(await archive_lead_form("456", access_token="token"))
    assert result == {"success": True}
    assert request.await_args.args[2] == {"status": "ARCHIVED"}
