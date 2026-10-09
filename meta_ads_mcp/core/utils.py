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

# The log file sits next to the token cache and records request activity, so it
# gets the same owner-only treatment. See GHSA-prmg-4fr3-mm6x for the cache.
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600

# Log level default. DEBUG used to be hard-coded, which (together with the root
# handler below) wrote a great deal of request detail to a file that is never
# rotated. Operators who need it set META_ADS_LOG_LEVEL=DEBUG.
DEFAULT_LOG_LEVEL = "INFO"

# httpx logs "HTTP Request: GET <url>" at INFO, and for the Graph API that URL
# carries `access_token=<the operator's token>` in the query string. The old
# logging.basicConfig() call installed a handler on the ROOT logger, so those
# records landed in meta_ads_debug.log in full. The handler is now attached to
# this package's logger only, and httpx is held at WARNING so the line is not
# emitted into whatever logging the host application configures either.
# See GHSA-r3r9-3mrh-x966.
_URL_LOGGING_LIBRARIES = ("httpx", "httpcore")


def restrict_permissions(path: pathlib.Path, mode: int) -> None:
    """Narrow `path` to `mode` if it is wider, best effort.

    POSIX only: on Windows chmod cannot express "owner only", and access is
    governed by ACLs instead.
    """
    if platform.system() == "Windows":
        return
    try:
        if (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
    except OSError:
        # Logging is not configured yet when this runs at import time, and a
        # permissions failure must never stop the server from starting.
        pass


def redact_secret(value: Optional[str]) -> str:
    """Render a credential for a log line or a tool response.

    Returns the length only. Prefixes (`token[:10]`) were used before, which is
    still credential material in a file that is never rotated, and long enough
    to correlate one caller's requests across a shared deployment.
    """
    if not value:
        return "<none>"
    return f"<redacted:{len(value)} chars>"


def _resolve_log_level() -> int:
    requested = os.environ.get("META_ADS_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    level = logging.getLevelName(requested)
    return level if isinstance(level, int) else logging.INFO


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
        log_dir.mkdir(parents=True, exist_ok=True, mode=LOG_DIR_MODE)
        restrict_permissions(log_dir, LOG_DIR_MODE)
        # Create the file ourselves so it exists with 0600 before the handler
        # opens it; narrow it afterwards in case an earlier version left it wider.
        if not log_file.exists():
            os.close(os.open(log_file, os.O_CREAT | os.O_WRONLY | os.O_APPEND, LOG_FILE_MODE))
        restrict_permissions(log_file, LOG_FILE_MODE)
        log_file_usable = True
    except OSError:
        # Read-only homes and hardened containers should still be able to run.
        log_file_usable = False

    # Create a logger
    logger = logging.getLogger("meta-ads-mcp")
    logger.setLevel(_resolve_log_level())

    # Attach the handler to this logger rather than the root logger, so
    # third-party records (notably httpx's full request URLs) are not captured.
    # stderr is the fallback: safe for HTTP and it does not corrupt stdio's
    # stdout channel.
    if log_file_usable:
        handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
        logging_destination = str(log_file)
    else:
        handler = logging.StreamHandler(sys.stderr)
        logging_destination = "stderr (log directory unavailable)"
    for old in [h for h in logger.handlers if getattr(h, "_meta_ads_mcp_handler", False)]:
        old.close()
        logger.removeHandler(old)
    handler._meta_ads_mcp_handler = True
    handler.setFormatter(
        logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    )
    logger.addHandler(handler)
    logger.propagate = False

    for library in _URL_LOGGING_LIBRARIES:
        logging.getLogger(library).setLevel(logging.WARNING)
    
    # Log startup information
    logger.info("Logging initialized. Destination: %s", logging_destination)
    logger.info(f"Platform: {platform.system()} {platform.release()}")
    logger.info(f"Using META_ACCESS_TOKEN from environment: {using_direct_token}")
    
    return logger

# Create the logger instance to be imported by other modules
logger = setup_logging()


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

# Non-public ranges named explicitly, because `is_global`'s backing table has
# changed across CPython patch releases and a category denylist alone missed
# them (GHSA-cx77-j6h8-3382: 100.64.0.0/10 is neither private nor reserved, so
# it passed the original check while hosting internal services on the
# deployments that use it — GKE pod/service CIDRs, cloud internal endpoints,
# ISP CGNAT).
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "100.64.0.0/10",     # RFC 6598 shared address space (CGNAT)
        "192.0.0.0/24",      # RFC 6890 IETF protocol assignments
        "192.0.2.0/24",      # RFC 5737 TEST-NET-1
        "198.18.0.0/15",     # RFC 2544 benchmarking
        "198.51.100.0/24",   # RFC 5737 TEST-NET-2
        "203.0.113.0/24",    # RFC 5737 TEST-NET-3
        "240.0.0.0/4",       # RFC 1112 reserved, incl. 255.255.255.255
        "2001:db8::/32",     # RFC 3849 documentation
        "64:ff9b:1::/48",    # RFC 8215 local-use IPv4/IPv6 translation
    )
)


def _ip_is_disallowed(ip) -> bool:
    """Return True if `ip` is not a public, routable address.

    The primary test is positive — an address must be `is_global` — so ranges
    that are simply not public (shared address space, benchmarking, TEST-NETs)
    are rejected without having to be enumerated. The category checks and the
    explicit network list are kept alongside it as belt and braces.

    IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1) are unwrapped first so
    they cannot smuggle a non-public IPv4 target past the check.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    if not ip.is_global:
        return True
    return any(ip in network for network in _BLOCKED_NETWORKS)


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
                f"({ip}). Private, loopback, link-local, shared (CGNAT), reserved "
                "and cloud-metadata addresses are blocked to prevent server-side "
                "request forgery."
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

