import json
from unittest.mock import AsyncMock, patch

import pytest

from meta_ads_mcp.core.ads import create_ad
from meta_ads_mcp.core.adsets import create_adset, validate_lead_campaign_configuration
from meta_ads_mcp.core.campaigns import create_campaign


@pytest.mark.asyncio
async def test_campaign_never_invents_budget_and_supports_validate_only():
    request = AsyncMock(return_value={"success": True})
    with patch("meta_ads_mcp.core.campaigns.make_api_request", request):
        await create_campaign(
            "act_123", "Leads", "OUTCOME_LEADS",
            special_ad_categories=[], special_ad_category_country=["AR"],
            use_adset_level_budgets=True, validate_only=True, access_token="token",
        )
    params = request.await_args.args[2]
    assert "daily_budget" not in params
    assert "lifetime_budget" not in params
    assert params["special_ad_category_country"] == '["AR"]'
    assert params["execution_options"] == ["validate_only"]


@pytest.mark.asyncio
async def test_special_category_requires_country():
    result = json.loads(await create_campaign(
        "act_123", "Housing leads", "OUTCOME_LEADS",
        special_ad_categories=["HOUSING"], access_token="token",
    ))
    assert "special_ad_category_country is required" in result["error"]


@pytest.mark.asyncio
async def test_adset_requires_explicit_geo_and_advantage_choice():
    with patch("meta_ads_mcp.core.adsets.make_api_request", AsyncMock(return_value={
        "id": "456", "objective": "OUTCOME_LEADS"
    })):
        result = json.loads(await create_adset(
            "act_123", "456", "Lead set", "LEAD_GENERATION", "IMPRESSIONS",
            targeting=None, access_token="token",
        ))
    assert "explicit targeting.geo_locations" in result["error"]


@pytest.mark.asyncio
async def test_lead_matrix_blocks_unsupported_click_to_messenger():
    result = json.loads(await validate_lead_campaign_configuration(
        "MESSENGER", "CONVERSATIONS"
    ))
    assert result["valid"] is False
    assert "not available" in result["errors"][0]


@pytest.mark.asyncio
async def test_website_conversion_leads_require_dataset_or_pixel():
    result = json.loads(await validate_lead_campaign_configuration(
        "WEBSITE", "OFFSITE_CONVERSIONS", promoted_object={}
    ))
    assert result["valid"] is False
    assert "pixel_id or dataset_id" in result["errors"][0]


@pytest.mark.asyncio
async def test_adset_sends_current_advantage_and_attribution_fields():
    request = AsyncMock(side_effect=[
        {"id": "456", "name": "Leads", "objective": "OUTCOME_LEADS"},
        {"id": "789"},
    ])
    targeting = {
        "geo_locations": {"countries": ["AR"]},
        "targeting_automation": {"advantage_audience": 1},
    }
    with patch("meta_ads_mcp.core.adsets.make_api_request", request):
        result = json.loads(await create_adset(
            "act_123", "456", "Lead set", "QUALITY_LEAD", "IMPRESSIONS",
            targeting=targeting, destination_type="ON_AD",
            automatic_manual_state="AUTOMATIC",
            is_incremental_attribution_enabled=True,
            full_funnel_exploration_mode="EXTENDED_EXPLORATION",
            validate_only=True, access_token="token",
        ))
    assert result["id"] == "789"
    params = request.await_args_list[1].args[2]
    assert params["automatic_manual_state"] == "AUTOMATIC"
    assert params["is_incremental_attribution_enabled"] is True
    assert params["execution_options"] == ["validate_only"]


@pytest.mark.asyncio
async def test_ad_supports_conversion_domain_and_validate_only():
    request = AsyncMock(return_value={"id": "999"})
    with patch("meta_ads_mcp.core.ads.make_api_request", request):
        await create_ad(
            "act_123", "Lead ad", "789", "creative",
            conversion_domain="example.com", validate_only=True, access_token="token",
        )
    params = request.await_args.args[2]
    assert params["conversion_domain"] == "example.com"
    assert params["execution_options"] == ["validate_only"]
