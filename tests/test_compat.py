"""Tests for version compatibility module."""

from prokube.common.compat import get_sdk_version, parse_version


class TestParseVersion:
    """Tests for parse_version function."""

    def test_simple_version(self):
        """Test parsing simple version."""
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_version_with_suffix(self):
        """Test parsing version with suffix."""
        assert parse_version("1.2.3-dev") == (1, 2, 3)
        assert parse_version("1.2.3-alpha.1") == (1, 2, 3)
        assert parse_version("1.2.3+build.123") == (1, 2, 3)

    def test_two_part_version(self):
        """Test parsing two-part version (normalized to 3 parts)."""
        assert parse_version("1.0") == (1, 0, 0)

    def test_single_part_version(self):
        """Test parsing single-part version (normalized to 3 parts)."""
        assert parse_version("1") == (1, 0, 0)

    def test_version_with_v_prefix(self):
        """Test parsing version with v prefix."""
        assert parse_version("v0.1.0") == (0, 1, 0)
        assert parse_version("V1.2.3") == (1, 2, 3)

    def test_version_with_rc_suffix(self):
        """Test parsing version with rc/beta suffix in component."""
        assert parse_version("1.2.3rc1") == (1, 2, 3)
        assert parse_version("1.2.3beta2") == (1, 2, 3)
        assert parse_version("1.2.0a1") == (1, 2, 0)


class TestGetSdkVersion:
    """Tests for get_sdk_version function."""

    def test_returns_string(self):
        """Test that SDK version is returned as string."""
        version = get_sdk_version()
        assert isinstance(version, str)
        assert len(version) > 0

    def test_version_format(self):
        """Test that version has expected format."""
        version = get_sdk_version()
        parts = version.split(".")
        assert len(parts) >= 2  # At least major.minor
        assert all(p.isdigit() for p in parts[:2])
