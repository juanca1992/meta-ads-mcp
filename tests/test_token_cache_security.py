"""Security tests for the on-disk OAuth token cache."""

import stat
from unittest.mock import patch

from meta_ads_mcp.core.auth import AuthManager, TokenInfo


def test_token_cache_uses_owner_only_permissions(tmp_path):
    with patch("pathlib.Path.home", return_value=tmp_path):
        manager = AuthManager("test-app-id")
        manager.token_info = TokenInfo(
            "FAKE_ACCESS_TOKEN_VALUE_FOR_TEST_123",
            expires_in=3600,
        )
        manager._save_token_to_cache()

        cache_dir = tmp_path / ".config" / "meta-ads-mcp"
        cache_file = cache_dir / "token_cache.json"

        assert cache_file.exists()
        assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600


def test_existing_token_cache_permissions_are_tightened(tmp_path):
    cache_dir = tmp_path / ".config" / "meta-ads-mcp"
    cache_dir.mkdir(parents=True, mode=0o755)
    cache_file = cache_dir / "token_cache.json"
    cache_file.write_text("{}")
    cache_file.chmod(0o644)

    with patch("pathlib.Path.home", return_value=tmp_path):
        manager = AuthManager("test-app-id")
        manager.token_info = TokenInfo(
            "FAKE_ACCESS_TOKEN_VALUE_FOR_TEST_123",
            expires_in=3600,
        )
        manager._save_token_to_cache()

    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
