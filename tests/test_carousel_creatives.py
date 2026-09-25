"""Regression tests for deterministic carousel ad creatives."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from meta_ads_mcp.core.ads import create_ad_creative


@pytest.mark.asyncio
async def test_carousel_serializes_ordered_child_attachments():
    """A carousel must use link_data.child_attachments, never asset_feed_spec."""
    with patch("meta_ads_mcp.core.ads.make_api_request", new_callable=AsyncMock) as mock_api, \
            patch(
                "meta_ads_mcp.core.ads._fetch_video_thumbnail_with_status",
                new_callable=AsyncMock,
                return_value=("https://cdn.example.com/thumb.jpg", "ready"),
            ):
        mock_api.side_effect = [
            {"id": "creative_123"},
            {"id": "creative_123", "status": "PAUSED"},
        ]

        result = json.loads(
            await create_ad_creative(
                account_id="act_123",
                access_token="test_token",
                page_id="456",
                name="Carousel test",
                link_url="https://example.com/catalog",
                message="Choose your favorite product",
                call_to_action_type="SHOP_NOW",
                carousel_cards=[
                    {
                        "image_hash": "hash_one",
                        "link_url": "https://example.com/product-one",
                        "headline": "Product one",
                        "description": "First card",
                    },
                    {
                        "video_id": "video_two",
                        "link": "https://example.com/product-two",
                        "name": "Product two",
                    },
                ],
            )
        )

    assert result["success"] is True
    posted = mock_api.call_args_list[0].args[2]
    assert "asset_feed_spec" not in posted
    assert posted["object_type"] == "SHARE"
    link_data = posted["object_story_spec"]["link_data"]
    assert link_data["link"] == "https://example.com/catalog"
    assert link_data["message"] == "Choose your favorite product"
    assert link_data["multi_share_optimized"] is False
    assert link_data["call_to_action"] == {
        "type": "SHOP_NOW",
        "value": {"link": "https://example.com/catalog"},
    }
    assert link_data["child_attachments"] == [
        {
            "link": "https://example.com/product-one",
            "name": "Product one",
            "image_hash": "hash_one",
            "description": "First card",
        },
        {
            "link": "https://example.com/product-two",
            "name": "Product two",
            "video_id": "video_two",
            "picture": "https://cdn.example.com/thumb.jpg",
        },
    ]


@pytest.mark.asyncio
async def test_carousel_requires_two_to_ten_cards():
    result = json.loads(
        await create_ad_creative(
            account_id="act_123",
            access_token="test_token",
            page_id="456",
            link_url="https://example.com/catalog",
            carousel_cards=[{"image_hash": "hash_one", "headline": "Only card"}],
        )
    )

    assert result["error"] == "carousel_cards must contain between 2 and 10 cards."


@pytest.mark.asyncio
async def test_carousel_rejects_media_or_dynamic_conflicts():
    result = json.loads(
        await create_ad_creative(
            account_id="act_123",
            access_token="test_token",
            page_id="456",
            link_url="https://example.com/catalog",
            image_hash="single_hash",
            carousel_cards=[
                {"image_hash": "hash_one", "headline": "One"},
                {"image_hash": "hash_two", "headline": "Two"},
            ],
        )
    )

    assert "Only one media source allowed" in result["error"]


@pytest.mark.asyncio
async def test_carousel_rejects_card_without_exactly_one_media_asset():
    result = json.loads(
        await create_ad_creative(
            account_id="act_123",
            access_token="test_token",
            page_id="456",
            link_url="https://example.com/catalog",
            carousel_cards=[
                {"image_hash": "hash_one", "headline": "One"},
                {"image_hash": "hash_two", "video_id": "video_two", "headline": "Two"},
            ],
        )
    )

    assert result["error"] == "Carousel card 2 must include exactly one of image_hash or video_id."


@pytest.mark.asyncio
async def test_carousel_rejects_lead_form_destination():
    """Graph API v26 rejects instant-form CTAs on carousel child attachments."""
    result = json.loads(
        await create_ad_creative(
            account_id="act_123",
            access_token="test_token",
            page_id="456",
            link_url="https://example.com/catalog",
            call_to_action_type="SIGN_UP",
            lead_gen_form_id="form_789",
            carousel_cards=[
                {"image_hash": "hash_one", "headline": "One"},
                {"image_hash": "hash_two", "headline": "Two"},
            ],
        )
    )

    assert "lead_gen_form_id" in result["error"]


@pytest.mark.asyncio
async def test_carousel_video_card_uses_explicit_thumbnail_hash():
    with patch("meta_ads_mcp.core.ads.make_api_request", new_callable=AsyncMock) as mock_api, \
            patch(
                "meta_ads_mcp.core.ads._fetch_video_thumbnail_with_status",
                new_callable=AsyncMock,
            ) as mock_thumb:
        mock_api.side_effect = [{"id": "creative_123"}, {"id": "creative_123"}]
        await create_ad_creative(
            account_id="act_123",
            access_token="test_token",
            page_id="456",
            link_url="https://example.com/catalog",
            carousel_cards=[
                {"image_hash": "hash_one", "headline": "One"},
                {"video_id": "video_two", "thumbnail_hash": "thumb_hash", "headline": "Two"},
            ],
        )

    mock_thumb.assert_not_called()
    cards = mock_api.call_args_list[0].args[2]["object_story_spec"]["link_data"]["child_attachments"]
    assert cards[1] == {"link": "https://example.com/catalog", "name": "Two",
                        "video_id": "video_two", "image_hash": "thumb_hash"}


@pytest.mark.asyncio
async def test_carousel_video_card_without_thumbnail_fails_clearly():
    with patch(
        "meta_ads_mcp.core.ads._fetch_video_thumbnail_with_status",
        new_callable=AsyncMock,
        return_value=(None, "processing"),
    ):
        result = json.loads(
            await create_ad_creative(
                account_id="act_123",
                access_token="test_token",
                page_id="456",
                link_url="https://example.com/catalog",
                carousel_cards=[
                    {"image_hash": "hash_one", "headline": "One"},
                    {"video_id": "video_two", "headline": "Two"},
                ],
            )
        )

    assert "no thumbnail available" in result["error"]
    assert result["video_status"] == "processing"


@pytest.mark.asyncio
async def test_carousel_rejects_creative_level_headline():
    result = json.loads(
        await create_ad_creative(
            account_id="act_123",
            access_token="test_token",
            page_id="456",
            link_url="https://example.com/catalog",
            headline="Ignored headline",
            carousel_cards=[
                {"image_hash": "hash_one", "headline": "One"},
                {"image_hash": "hash_two", "headline": "Two"},
            ],
        )
    )

    assert "headline" in result["error"]
