"""Utility functions for Meta Ads API."""

from .security import diagnostic_print as print

from typing import Optional, Dict, Any, List
import httpx
import io
from PIL import Image as PILImage
import base64
import time
import asyncio
import os
import json
import logging
import pathlib
import platform
import ipaddress
import socket
import sys
from urllib.parse import urlparse

from .security import install_log_redaction
install_log_redaction()

# Check for Meta app credentials in environment
META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")

# A direct META_ACCESS_TOKEN needs neither an app id nor an app secret, so these
# warnings only apply when we might have to run the local OAuth flow.
using_direct_token = bool(os.environ.get("META_ACCESS_TOKEN", ""))

# Written to stderr on purpose: stdout is the JSON-RPC channel on the stdio
# transport, so printing there corrupts the protocol stream.
if not using_direct_token:
    if not META_APP_ID:
        print("WARNING: META_APP_ID environment variable is not set.", file=sys.stderr)
        print("RECOMMENDED: Set META_ACCESS_TOKEN to a token from your own Meta app.", file=sys.stderr)
        print("ALTERNATIVE: Set META_APP_ID to your Meta App ID to use the local OAuth flow.", file=sys.stderr)
    if not META_APP_SECRET:
        print("WARNING: META_APP_SECRET environment variable is not set.", file=sys.stderr)
        print("NOTE: This is only needed to exchange a short-lived token for a long-lived one.", file=sys.stderr)

# Configure logging to file
def setup_logging():
    """Set up owner-only file logging, with a safe stderr fallback."""
    # Get platform-specific path for logs
    if platform.system() == "Windows":
        base_path = pathlib.Path(os.environ.get("APPDATA", ""))
    elif platform.system() == "Darwin":  # macOS
        base_path = pathlib.Path.home() / "Library" / "Application Support"
    else:  # Assume Linux/Unix
        base_path = pathlib.Path.home() / ".config"
    
    log_dir = base_path / "meta-ads-mcp"
    log_file = log_dir / "meta_ads_debug.log"

    try:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_dir.chmod(0o700)
        handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        log_file.chmod(0o600)
        logging_destination = str(log_file)
    except OSError:
        # Read-only homes and hardened containers should still be able to run.
        # stderr is safe for HTTP and does not corrupt stdio's stdout channel.
        handler = logging.StreamHandler(sys.stderr)
        logging_destination = "stderr (log directory unavailable)"

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[handler],
    )
    
    # Create a logger
    logger = logging.getLogger("meta-ads-mcp")
    logger.setLevel(logging.INFO)
    
    # Log startup information
    logger.info("Logging initialized. Destination: %s", logging_destination)
    logger.info(f"Platform: {platform.system()} {platform.release()}")
    logger.info(f"Using META_ACCESS_TOKEN from environment: {using_direct_token}")
    
    return logger

# Create the logger instance to be imported by other modules
logger = setup_logging()

# Global store for ad creative images
ad_creative_images = {}


def extract_creative_image_urls(creative: Dict[str, Any]) -> List[str]:
    """
    Extract image URLs from a creative object for direct viewing.
    Prioritizes higher quality images over thumbnails.
    
    Args:
        creative: Meta Ads creative object
        
    Returns:
        List of image URLs found in the creative, prioritized by quality
    """
    image_urls = []
    
    # Prioritize higher quality image URLs in this order:
    # 1. image_urls_for_viewing (usually highest quality)
    # 2. image_url (direct field)
    # 3. object_story_spec.link_data.picture (usually full size)
    # 4. asset_feed_spec images (multiple high-quality images)
    # 5. thumbnail_url (last resort - often profile thumbnail)
    
    # Check for image_urls_for_viewing (highest priority)
    if "image_urls_for_viewing" in creative and creative["image_urls_for_viewing"]:
        image_urls.extend(creative["image_urls_for_viewing"])
    
    # Check for direct image_url field
    if "image_url" in creative and creative["image_url"]:
        image_urls.append(creative["image_url"])
    
    # Check object_story_spec for image URLs
    if "object_story_spec" in creative:
        story_spec = creative["object_story_spec"]
        
        # Check link_data for image fields
        if "link_data" in story_spec:
            link_data = story_spec["link_data"]
            
            # Check for picture field (usually full size)
            if "picture" in link_data and link_data["picture"]:
                image_urls.append(link_data["picture"])
                
            # Check for image_url field in link_data
            if "image_url" in link_data and link_data["image_url"]:
                image_urls.append(link_data["image_url"])
        
        # Check video_data for thumbnail (if present)
        if "video_data" in story_spec and "image_url" in story_spec["video_data"]:
            image_urls.append(story_spec["video_data"]["image_url"])
    
    # Check asset_feed_spec for multiple images
    if "asset_feed_spec" in creative and "images" in creative["asset_feed_spec"]:
        for image in creative["asset_feed_spec"]["images"]:
            if "url" in image and image["url"]:
                image_urls.append(image["url"])
    
    # Check for thumbnail_url field (lowest priority)
    if "thumbnail_url" in creative and creative["thumbnail_url"]:
        image_urls.append(creative["thumbnail_url"])
    
    # Remove duplicates while preserving order
    seen = set()
    unique_urls = []
    for url in image_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    
    return unique_urls


# --- Server-side request forgery (SSRF) guard for outbound image fetches ---
#
# upload_ad_image and the image-viewing tools fetch a caller-supplied URL
# server-side. Without validation an attacker could point the URL at internal
# services (http://127.0.0.1/...), private networks (10.x/192.168.x/172.16.x),
# or the cloud metadata endpoint (http://169.254.169.254/) and use the server
# as a proxy. See GHSA-45gf-fjxp-cjpq.
#
# Connection-time validation and literal-IP pinning are implemented in
# download_transport.py; redirects pass through the same checks.

class BlockedURLError(Exception):
    """Raised when a URL targets a disallowed (non-public) address."""


_ALLOWED_URL_SCHEMES = ("http", "https")


def _ip_is_disallowed(ip) -> bool:
    """Return True if `ip` is not a public, routable address.

    Blocks private, loopback, link-local (incl. 169.254.169.254 cloud
    metadata), reserved, multicast, and unspecified addresses. IPv4-mapped
    IPv6 addresses (e.g. ::ffff:127.0.0.1) are unwrapped first so they can't
    be used to smuggle a private IPv4 target past the check.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_public_url(url: str, resolve: bool = True) -> None:
    """Validate that `url` is safe to fetch from the server (SSRF guard).

    Raises BlockedURLError if the URL is not http(s), has no host, or resolves
    to any non-public address. A literal-IP host is checked directly; a
    hostname is resolved and every returned address must be public.
    """
    if not url or not isinstance(url, str):
        raise BlockedURLError("No URL provided")

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise BlockedURLError(
            f"URL scheme '{parsed.scheme}' is not allowed; "
            "only http and https URLs can be fetched"
        )

    if parsed.username is not None or parsed.password is not None:
        raise BlockedURLError("Credentials in download URLs are not allowed")
    host = parsed.hostname
    if not host:
        raise BlockedURLError("URL has no host")

    try:
        candidate_ips = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return  # The network backend validates DNS asynchronously at connection time.
        # Not a literal IP — resolve the hostname and check every address.
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise BlockedURLError(f"Could not resolve host '{host}': {e}")
        candidate_ips = []
        for info in infos:
            ip_text = info[4][0].split("%")[0]  # strip any IPv6 scope id
            try:
                candidate_ips.append(ipaddress.ip_address(ip_text))
            except ValueError:
                continue
        if not candidate_ips:
            raise BlockedURLError(f"Could not resolve host '{host}' to any IP address")

    for ip in candidate_ips:
        if _ip_is_disallowed(ip):
            raise BlockedURLError(
                f"Refusing to fetch '{host}': it resolves to a non-public address "
                f"({ip}). Private, loopback, link-local, and cloud-metadata "
                "addresses are blocked to prevent server-side request forgery."
            )


async def _ssrf_guard_request_hook(request: "httpx.Request") -> None:
    """httpx request event hook that re-validates every outbound request.

    Fires for the initial request and for each redirect hop, so a public URL
    cannot redirect into a private/internal address.
    """
    validate_public_url(str(request.url), resolve=False)


MAX_IMAGE_BYTES = 20 * 1024 * 1024
_download_slots = asyncio.Semaphore(4)


async def _bounded_download(url):
    from .download_transport import PublicHTTPTransport
    async with _download_slots:
        async with httpx.AsyncClient(
            transport=PublicHTTPTransport(), trust_env=False, follow_redirects=True,
            max_redirects=5, timeout=30,
            event_hooks={"request": [_ssrf_guard_request_hook]},
        ) as client:
            async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed image responses are not accepted")
                if int(response.headers.get("content-length", "0")) > MAX_IMAGE_BYTES:
                    raise ValueError("Image exceeds download limit")
                result = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    if len(result) + len(chunk) > MAX_IMAGE_BYTES:
                        raise ValueError("Image exceeds download limit")
                    result.extend(chunk)
                return bytes(result)


async def download_image(url: str) -> Optional[bytes]:
    """Fetch up to 20 MiB in 60 seconds, with public-IP pinning and no proxy."""
    try:
        validate_public_url(url, resolve=False)
        return await asyncio.wait_for(_bounded_download(url), timeout=60)
    except Exception as exc:
        logger.warning("Image download rejected or failed (%s)", type(exc).__name__)
        return None


async def try_multiple_download_methods(url: str) -> Optional[bytes]:
    """Compatibility entry point: one bounded, validated download, no bypasses."""
    validate_public_url(url, resolve=False)
    return await download_image(url)


def create_resource_from_image(image_bytes: bytes, resource_id: str, name: str) -> Dict[str, Any]:
    """
    Create a resource entry from image bytes.
    
    Args:
        image_bytes: Raw image data
        resource_id: Unique identifier for the resource
        name: Human-readable name for the resource
        
    Returns:
        Dictionary with resource information
    """
    ad_creative_images[resource_id] = {
        "data": image_bytes,
        "mime_type": "image/jpeg",
        "name": name
    }
    
    return {
        "resource_id": resource_id,
        "resource_uri": f"meta-ads://images/{resource_id}",
        "name": name,
        "size": len(image_bytes)
    }
