"""Instant-form and lead-delivery tools for Meta Lead Ads (Graph API v26)."""

import base64
import binascii
import io
import json
from typing import Any, Dict, List, Optional

from .api import make_api_request, meta_api_tool
from .server import mcp_server


_FORM_FIELDS = (
    "id,name,status,locale,created_time,page_id,privacy_policy_url,questions,"
    "legal_content,thank_you_page,context_card,tracking_parameters,"
    "is_optimized_for_quality,block_display_for_non_targeted_viewer,leads_count"
)
_LEAD_FIELDS = (
    "id,created_time,field_data,ad_id,ad_name,adset_id,adset_name,"
    "campaign_id,campaign_name,form_id,is_organic,platform,retailer_item_id"
)


def _clean_id(value: str, label: str) -> Optional[str]:
    value = str(value or "").strip()
    return value if value and value.isascii() and value.isdigit() else None


@mcp_server.tool()
@meta_api_tool
async def get_leadgen_eligibility(page_id: str, access_token: Optional[str] = None) -> str:
    """Check whether a Page has accepted Meta Lead Generation Terms.

    This is a read-only preflight. A false value must be resolved by an
    authorized person in Meta; this tool never accepts legal terms for them.
    """
    page_id = _clean_id(page_id, "page_id")
    if not page_id:
        return json.dumps({"error": "page_id must be a numeric Meta Page ID"})
    data = await make_api_request(
        page_id,
        access_token,
        {"fields": "id,name,leadgen_tos_accepted,leadgen_tos_acceptance_time,leadgen_tos_accepting_user"},
    )
    if not data.get("error"):
        data["eligible_for_lead_ads"] = bool(data.get("leadgen_tos_accepted"))
        if not data["eligible_for_lead_ads"]:
            data["action_required"] = "An authorized Page user must accept Meta Lead Generation Terms."
    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_lead_forms(
    page_id: str,
    status: Optional[str] = None,
    limit: int = 25,
    after: str = "",
    access_token: Optional[str] = None,
) -> str:
    """List instant forms owned by a Facebook Page."""
    page_id = _clean_id(page_id, "page_id")
    if not page_id:
        return json.dumps({"error": "page_id must be a numeric Meta Page ID"})
    params: Dict[str, Any] = {"fields": _FORM_FIELDS, "limit": max(1, min(limit, 100))}
    if status:
        params["filtering"] = [{"field": "status", "operator": "EQUAL", "value": status.upper()}]
    if after:
        params["after"] = after
    return json.dumps(await make_api_request(f"{page_id}/leadgen_forms", access_token, params), indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_lead_form(lead_gen_form_id: str, access_token: Optional[str] = None) -> str:
    """Get an instant form, including questions, legal content, and thank-you page."""
    form_id = _clean_id(lead_gen_form_id, "lead_gen_form_id")
    if not form_id:
        return json.dumps({"error": "lead_gen_form_id must be numeric"})
    return json.dumps(
        await make_api_request(form_id, access_token, {"fields": _FORM_FIELDS}), indent=2
    )


@mcp_server.tool()
@meta_api_tool
async def create_lead_form(
    page_id: str,
    name: str,
    locale: str,
    questions: List[Dict[str, Any]],
    privacy_policy: Optional[Dict[str, Any]] = None,
    privacy_policy_url: Optional[str] = None,
    privacy_policy_link_text: str = "Privacy Policy",
    context_card: Optional[Dict[str, Any]] = None,
    custom_disclaimer: Optional[Dict[str, Any]] = None,
    thank_you_page: Optional[Dict[str, Any]] = None,
    follow_up_action_url: Optional[str] = None,
    question_page_custom_headline: Optional[str] = None,
    tracking_parameters: Optional[Dict[str, Any]] = None,
    is_optimized_for_quality: Optional[bool] = None,
    block_display_for_non_targeted_viewer: Optional[bool] = None,
    is_phone_sms_verify_enabled: Optional[bool] = None,
    should_enforce_work_email: Optional[bool] = None,
    is_lead_capture_ai_agent_enabled: Optional[bool] = None,
    gated_file_base64: Optional[str] = None,
    gated_file_name: str = "gated-content.pdf",
    access_token: Optional[str] = None,
) -> str:
    """Create a Page-scoped Meta instant form using the current v26 fields.

    Questions and legal/thank-you objects use Meta's native JSON shapes. Supply
    either privacy_policy or privacy_policy_url. A gated PDF may be sent as
    base64 (20 MiB maximum). Meta creates the form; publishing/eligibility still
    depends on Page permissions, Lead Ads Terms, and Meta review.
    """
    page_id = _clean_id(page_id, "page_id")
    if not page_id:
        return json.dumps({"error": "page_id must be a numeric Meta Page ID"})
    if not name or not locale or not questions:
        return json.dumps({"error": "name, locale, and at least one question are required"})
    if privacy_policy and privacy_policy_url:
        return json.dumps({"error": "Provide privacy_policy or privacy_policy_url, not both"})
    if not privacy_policy and not privacy_policy_url:
        return json.dumps({"error": "A privacy policy is required"})

    params: Dict[str, Any] = {"name": name, "locale": locale.upper(), "questions": questions}
    params["privacy_policy"] = privacy_policy or {
        "url": privacy_policy_url,
        "link_text": privacy_policy_link_text,
    }
    optional_values = {
        "context_card": context_card,
        "custom_disclaimer": custom_disclaimer,
        "thank_you_page": thank_you_page,
        "follow_up_action_url": follow_up_action_url,
        "question_page_custom_headline": question_page_custom_headline,
        "tracking_parameters": tracking_parameters,
        "is_optimized_for_quality": is_optimized_for_quality,
        "block_display_for_non_targeted_viewer": block_display_for_non_targeted_viewer,
        "is_phone_sms_verify_enabled": is_phone_sms_verify_enabled,
        "should_enforce_work_email": should_enforce_work_email,
        "is_lead_capture_ai_agent_enabled": is_lead_capture_ai_agent_enabled,
    }
    params.update({key: value for key, value in optional_values.items() if value is not None})

    files = None
    stream = None
    if gated_file_base64:
        try:
            payload = gated_file_base64.partition(",")[2] if gated_file_base64.startswith("data:") else gated_file_base64
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            return json.dumps({"error": "gated_file_base64 is not valid base64"})
        if not raw.startswith(b"%PDF-") or len(raw) > 20 * 1024 * 1024:
            return json.dumps({"error": "Gated content must be a PDF no larger than 20 MiB"})
        stream = io.BytesIO(raw)
        files = {"upload_gated_file": (gated_file_name, stream, "application/pdf")}

    try:
        data = await make_api_request(
            f"{page_id}/leadgen_forms", access_token, params, method="POST", files=files, timeout=120.0
        )
    finally:
        if stream is not None:
            stream.close()
    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def update_lead_form(
    lead_gen_form_id: str, status: str, access_token: Optional[str] = None
) -> str:
    """Update the supported mutable instant-form field: status."""
    form_id = _clean_id(lead_gen_form_id, "lead_gen_form_id")
    normalized = str(status or "").upper()
    if not form_id:
        return json.dumps({"error": "lead_gen_form_id must be numeric"})
    if normalized not in {"ACTIVE", "ARCHIVED"}:
        return json.dumps({"error": "status must be ACTIVE or ARCHIVED"})
    return json.dumps(
        await make_api_request(form_id, access_token, {"status": normalized}, method="POST"), indent=2
    )


@mcp_server.tool()
@meta_api_tool
async def archive_lead_form(lead_gen_form_id: str, access_token: Optional[str] = None) -> str:
    """Archive an instant form. Forms are retained for reporting and lead access."""
    form_id = _clean_id(lead_gen_form_id, "lead_gen_form_id")
    if not form_id:
        return json.dumps({"error": "lead_gen_form_id must be numeric"})
    return json.dumps(
        await make_api_request(form_id, access_token, {"status": "ARCHIVED"}, method="POST"), indent=2
    )


@mcp_server.tool()
@meta_api_tool
async def get_leads(
    lead_gen_form_id: str,
    limit: int = 25,
    after: str = "",
    access_token: Optional[str] = None,
) -> str:
    """Retrieve leads submitted to one instant form. Requires leads_retrieval."""
    form_id = _clean_id(lead_gen_form_id, "lead_gen_form_id")
    if not form_id:
        return json.dumps({"error": "lead_gen_form_id must be numeric"})
    params: Dict[str, Any] = {"fields": _LEAD_FIELDS, "limit": max(1, min(limit, 100))}
    if after:
        params["after"] = after
    return json.dumps(await make_api_request(f"{form_id}/leads", access_token, params), indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_ad_leads(
    ad_id: str, limit: int = 25, after: str = "", access_token: Optional[str] = None
) -> str:
    """Retrieve leads attributed to one ad. Requires leads_retrieval."""
    ad_id = _clean_id(ad_id, "ad_id")
    if not ad_id:
        return json.dumps({"error": "ad_id must be numeric"})
    params: Dict[str, Any] = {"fields": _LEAD_FIELDS, "limit": max(1, min(limit, 100))}
    if after:
        params["after"] = after
    return json.dumps(await make_api_request(f"{ad_id}/leads", access_token, params), indent=2)


@mcp_server.tool()
@meta_api_tool
async def create_test_lead(
    lead_gen_form_id: str,
    field_data: List[Dict[str, Any]],
    custom_disclaimer_responses: Optional[List[Dict[str, Any]]] = None,
    access_token: Optional[str] = None,
) -> str:
    """Create a test submission for an instant form; never creates a real prospect."""
    form_id = _clean_id(lead_gen_form_id, "lead_gen_form_id")
    if not form_id or not field_data:
        return json.dumps({"error": "A numeric lead_gen_form_id and field_data are required"})
    params: Dict[str, Any] = {"field_data": field_data}
    if custom_disclaimer_responses is not None:
        params["custom_disclaimer_responses"] = custom_disclaimer_responses
    return json.dumps(
        await make_api_request(f"{form_id}/test_leads", access_token, params, method="POST"), indent=2
    )


@mcp_server.tool()
@meta_api_tool
async def subscribe_page_leadgen_webhook(page_id: str, access_token: Optional[str] = None) -> str:
    """Subscribe the caller's Meta app to the Page's leadgen webhook field."""
    page_id = _clean_id(page_id, "page_id")
    if not page_id:
        return json.dumps({"error": "page_id must be a numeric Meta Page ID"})
    return json.dumps(
        await make_api_request(
            f"{page_id}/subscribed_apps", access_token, {"subscribed_fields": ["leadgen"]}, method="POST"
        ), indent=2
    )


@mcp_server.tool()
@meta_api_tool
async def unsubscribe_page_leadgen_webhook(page_id: str, access_token: Optional[str] = None) -> str:
    """Remove the caller's Meta app subscription from a Page."""
    page_id = _clean_id(page_id, "page_id")
    if not page_id:
        return json.dumps({"error": "page_id must be a numeric Meta Page ID"})
    return json.dumps(
        await make_api_request(f"{page_id}/subscribed_apps", access_token, method="DELETE"), indent=2
    )
