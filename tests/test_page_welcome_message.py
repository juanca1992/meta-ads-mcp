"""page_welcome_message must reach Meta as VISUAL_EDITOR JSON.

Plain text is accepted when the creative is created, but every ad created with
that creative then fails with code 2 ("An unexpected error has occurred",
is_transient=true) on click-to-WhatsApp ad sets.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from meta_ads_mcp.core.ads import _normalize_page_welcome_message, create_ad, create_ad_creative


def test_plain_text_is_wrapped_into_visual_editor_json():
    result = json.loads(_normalize_page_welcome_message("Hi! How can we help?"))
    assert result["type"] == "VISUAL_EDITOR"
    message = result["text_format"]["message"]
    assert message["text"] == "Hi! How can we help?"
    assert message["autofill_message"]["content"] == "Hi! How can we help?"


def test_json_string_is_passed_through_unchanged():
    raw = json.dumps({"type": "VISUAL_EDITOR", "version": 2, "template_id": "1"})
    assert _normalize_page_welcome_message(raw) == raw


def test_dict_is_serialized():
    assert json.loads(_normalize_page_welcome_message({"type": "VISUAL_EDITOR"})) == {"type": "VISUAL_EDITOR"}


def test_none_stays_none():
    assert _normalize_page_welcome_message(None) is None


@pytest.mark.asyncio
async def test_create_ad_creative_sends_wrapped_welcome_message():
    with patch("meta_ads_mcp.core.ads.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_123"}, {"id": "creative_123", "status": "ACTIVE"}]

        await create_ad_creative(
            account_id="act_123",
            image_hash="hash_123",
            page_id="page_123",
            link_url="https://api.whatsapp.com/send",
            message="Body",
            call_to_action_type="WHATSAPP_MESSAGE",
            page_welcome_message="Hi! How can we help?",
            access_token="test_token",
        )

        creative_data = mock_api.call_args_list[0][0][2]
        sent = json.loads(creative_data["page_welcome_message"])
        assert sent["type"] == "VISUAL_EDITOR"
        assert sent["text_format"]["message"]["text"] == "Hi! How can we help?"


@pytest.mark.asyncio
async def test_create_ad_adds_hint_for_unowned_catalog():
    meta_error = {
        "error": {
            "message": "HTTP Error: 400",
            "details": {"error": {"code": 100, "error_subcode": 3858114}},
        }
    }
    with patch("meta_ads_mcp.core.ads.make_api_request", new_callable=AsyncMock, return_value=meta_error):
        result = json.loads(await create_ad(
            account_id="act_123",
            name="Ad",
            adset_id="adset_123",
            creative_id="creative_123",
            access_token="test_token",
        ))

    assert "disable_all_enhancements" in result["hint"]
