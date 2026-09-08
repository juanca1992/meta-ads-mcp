"""Local file and diagnostic boundaries; no network or application imports."""
import builtins
import logging
import os
from pathlib import Path
import re
import stat
import sys
import traceback


def redact(text):
    text = str(text)
    secrets = [os.environ.get(key) for key in ("META_ACCESS_TOKEN", "META_APP_SECRET", "PIPEBOARD_API_TOKEN")]
    # Do not import application modules during logging initialization.
    context = sys.modules.get("meta_ads_mcp.core.http_auth_integration")
    if context is not None:
        for name in ("_auth_token", "_pipeboard_token"):
            variable = getattr(context, name, None)
            if variable is not None:
                secrets.append(variable.get(None))
    for secret in secrets:
        if secret:
            text = text.replace(secret, "REDACTED")
    text = re.sub(r"(?i)(bearer\s+)[^\s'\"&,}]+", r"\1REDACTED", text)
    text = re.sub(r"(?i)((?:access_token|appsecret_proof|client_secret|fb_exchange_token)(?:%22|['\"])?(?:%3A|:|=)\s*(?:%22|['\"])?)[^\s&'\",}]+", r"\1REDACTED", text)
    text = re.sub(r"(?i)([?&](?:code|state)=)[^&\s]+", r"\1REDACTED", text)
    return text


def install_log_redaction():
    factory = logging.getLogRecordFactory()
    if getattr(factory, "_meta_redaction", False):
        return

    def safe_factory(*args, **kwargs):
        record = factory(*args, **kwargs)
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return record

    safe_factory._meta_redaction = True
    logging.setLogRecordFactory(safe_factory)


def diagnostic_print(*args, **kwargs):
    kwargs["file"] = sys.stderr
    builtins.print(*(redact(arg) for arg in args), **kwargs)


def open_upload_file(file_path):
    """Open below a trusted root with no symlink traversal or FIFO blocking.

    Descriptor-relative walks keep concurrent renames from redirecting a checked
    path outside the root. On unsupported platforms local reads fail closed.
    """
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("Secure local uploads are unavailable on this platform; use base64")
    path = Path(file_path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("file_path must be absolute without parent traversal")
    roots = [Path(p).expanduser().resolve() for p in os.environ.get("META_ADS_VIDEO_UPLOAD_ROOTS", "").split(os.pathsep) if p.strip()]
    for root in roots:
        if not path.is_relative_to(root):
            continue
        parts = path.relative_to(root).parts
        if not parts:
            break
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("file_path must be a regular video file")
                return os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(directory)
    raise ValueError("Local path is outside META_ADS_VIDEO_UPLOAD_ROOTS")
