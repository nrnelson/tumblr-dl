"""Unit tests for the configuration module."""

from __future__ import annotations

from pathlib import Path

import pytest

from tumblr_dl.config import (
    AppConfig,
    AuthCredentials,
    BlogConfig,
    _parse_app_settings,
    load_auth,
    load_toml_config,
    resolve_blog_config,
    resolve_config_path,
)
from tumblr_dl.exceptions import ConfigError

_VALID_AUTH_TOML = """\
[auth]
consumer_key = "ck"
consumer_secret = "cs"
oauth_token = "ot"
oauth_token_secret = "os"
"""

_FULL_CONFIG_TOML = """\
[auth]
consumer_key = "ck"
consumer_secret = "cs"
oauth_token = "ot"
oauth_token_secret = "os"

[defaults]
output_dir = "media"
exclude_tags = ["gore*", "explicit"]
max_posts = 1000

[blog.myblog]
output_dir = "~/media/myblog"
exclude_tags = ["nsfw"]
max_posts = 500

[blog.otherblog]
full_scan = true
"""


# --- resolve_config_path ---


def test_resolve_config_path_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finds config.toml via XDG_CONFIG_HOME."""
    config_dir = tmp_path / "tumblr-dl"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text("[auth]\n")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert resolve_config_path() == config_file


def test_resolve_config_path_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falls back to ~/.config/tumblr-dl/config.toml."""
    config_dir = tmp_path / ".config" / "tumblr-dl"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "config.toml"
    config_file.write_text("[auth]\n")

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert resolve_config_path() == config_file


def test_resolve_config_path_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returns None when no config file exists."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert resolve_config_path() is None


# --- load_auth ---


def test_load_auth_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables take priority over TOML."""
    monkeypatch.setenv("TUMBLR_CONSUMER_KEY", "env_ck")
    monkeypatch.setenv("TUMBLR_CONSUMER_SECRET", "env_cs")
    monkeypatch.setenv("TUMBLR_OAUTH_TOKEN", "env_ot")
    monkeypatch.setenv("TUMBLR_OAUTH_TOKEN_SECRET", "env_os")

    toml_auth = AuthCredentials(
        consumer_key="toml_ck",
        consumer_secret="toml_cs",
        oauth_token="toml_ot",
        oauth_token_secret="toml_os",
    )
    app_config = AppConfig(auth=toml_auth)

    result = load_auth(app_config)
    assert result.consumer_key == "env_ck"
    assert result.oauth_token == "env_ot"


def test_load_auth_from_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falls back to TOML [auth] when env vars not set."""
    for var in [
        "TUMBLR_CONSUMER_KEY",
        "TUMBLR_CONSUMER_SECRET",
        "TUMBLR_OAUTH_TOKEN",
        "TUMBLR_OAUTH_TOKEN_SECRET",
    ]:
        monkeypatch.delenv(var, raising=False)

    toml_auth = AuthCredentials(
        consumer_key="toml_ck",
        consumer_secret="toml_cs",
        oauth_token="toml_ot",
        oauth_token_secret="toml_os",
    )
    result = load_auth(AppConfig(auth=toml_auth))
    assert result.consumer_key == "toml_ck"


def test_load_auth_partial_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial env vars raise ConfigError listing missing ones."""
    monkeypatch.setenv("TUMBLR_CONSUMER_KEY", "ck")
    monkeypatch.delenv("TUMBLR_CONSUMER_SECRET", raising=False)
    monkeypatch.delenv("TUMBLR_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("TUMBLR_OAUTH_TOKEN_SECRET", raising=False)

    with pytest.raises(ConfigError, match="Partial OAuth env vars") as exc_info:
        load_auth(None)

    assert "TUMBLR_CONSUMER_SECRET" in exc_info.value.context["missing"]


def test_load_auth_no_sources_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No auth source raises ConfigError with helpful message."""
    for var in [
        "TUMBLR_CONSUMER_KEY",
        "TUMBLR_CONSUMER_SECRET",
        "TUMBLR_OAUTH_TOKEN",
        "TUMBLR_OAUTH_TOKEN_SECRET",
    ]:
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ConfigError, match="No OAuth credentials found"):
        load_auth(None)


# --- load_toml_config ---


def test_load_toml_config_full(tmp_path: Path) -> None:
    """Parses a full config with auth, defaults, and per-blog sections."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(_FULL_CONFIG_TOML)

    app = load_toml_config(config_file)

    assert app.auth is not None
    assert app.auth.consumer_key == "ck"
    assert app.defaults.output_dir == "media"
    assert app.defaults.exclude_tags == ["gore*", "explicit"]
    assert app.defaults.max_posts == 1000
    assert "myblog" in app.blogs
    assert app.blogs["myblog"].output_dir == "~/media/myblog"
    assert app.blogs["myblog"].exclude_tags == ["nsfw"]
    assert app.blogs["myblog"].max_posts == 500
    assert "otherblog" in app.blogs
    assert app.blogs["otherblog"].full_scan is True


def test_load_toml_config_auth_only(tmp_path: Path) -> None:
    """Config with only [auth] parses correctly."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(_VALID_AUTH_TOML)

    app = load_toml_config(config_file)
    assert app.auth is not None
    assert app.auth.consumer_key == "ck"
    assert not app.blogs


def test_load_toml_config_missing_file(tmp_path: Path) -> None:
    """Missing config file raises ConfigError."""
    with pytest.raises(ConfigError, match="Config file not found"):
        load_toml_config(tmp_path / "nope.toml")


def test_load_toml_config_invalid_toml(tmp_path: Path) -> None:
    """Malformed TOML raises ConfigError."""
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text("{{invalid toml")

    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_toml_config(bad_file)


def test_load_toml_config_incomplete_auth(tmp_path: Path) -> None:
    """Partial [auth] section raises ConfigError."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[auth]\nconsumer_key = "ck"\n')

    with pytest.raises(ConfigError, match="Incomplete \\[auth\\] section"):
        load_toml_config(config_file)


def test_load_toml_config_bad_type(tmp_path: Path) -> None:
    """Wrong type for a config key raises ConfigError."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[defaults]\nexclude_tags = "not a list"\n')

    with pytest.raises(ConfigError, match="must be a list"):
        load_toml_config(config_file)


# --- resolve_blog_config ---


def test_resolve_blog_config_defaults_only() -> None:
    """With no config, returns hardcoded defaults."""
    config = resolve_blog_config("anyblog", None, {})
    assert config.output_dir == "tumblr_downloads"
    assert config.exclude_tags == []
    assert config.max_posts is None


def test_resolve_blog_config_toml_defaults() -> None:
    """TOML defaults override hardcoded defaults."""
    app = AppConfig(
        defaults=BlogConfig(output_dir="media", max_posts=500),
    )
    config = resolve_blog_config("anyblog", app, {})
    assert config.output_dir == "media"
    assert config.max_posts == 500


def test_resolve_blog_config_per_blog_overrides_defaults() -> None:
    """Per-blog section overrides defaults."""
    app = AppConfig(
        defaults=BlogConfig(output_dir="media", max_posts=500),
        blogs={"myblog": BlogConfig(output_dir="~/myblog", max_posts=100)},
    )
    config = resolve_blog_config("myblog", app, {})
    assert config.output_dir == "~/myblog"
    assert config.max_posts == 100


def test_resolve_blog_config_cli_overrides_all() -> None:
    """CLI overrides take precedence over TOML."""
    app = AppConfig(
        defaults=BlogConfig(output_dir="media"),
        blogs={"myblog": BlogConfig(output_dir="~/myblog")},
    )
    config = resolve_blog_config("myblog", app, {"output_dir": "/tmp/override"})
    assert config.output_dir == "/tmp/override"


def test_resolve_blog_config_cli_none_does_not_override() -> None:
    """CLI value of None does not override TOML."""
    app = AppConfig(
        defaults=BlogConfig(output_dir="media"),
    )
    config = resolve_blog_config("anyblog", app, {"output_dir": None})
    assert config.output_dir == "media"


def test_resolve_blog_config_unknown_blog_uses_defaults() -> None:
    """Blog not in config uses defaults only."""
    app = AppConfig(
        defaults=BlogConfig(output_dir="media", exclude_tags=["nsfw"]),
        blogs={"myblog": BlogConfig(output_dir="~/myblog")},
    )
    config = resolve_blog_config("otherblog", app, {})
    assert config.output_dir == "media"
    assert config.exclude_tags == ["nsfw"]


# --- _parse_app_settings ---


def test_parse_app_settings_max_concurrent_valid() -> None:
    """Valid max_concurrent value is parsed."""
    settings = _parse_app_settings({"max_concurrent": 8})
    assert settings.max_concurrent == 8


def test_parse_app_settings_max_concurrent_default() -> None:
    """Default max_concurrent is 4 when not specified."""
    settings = _parse_app_settings({})
    assert settings.max_concurrent == 4


def test_parse_app_settings_max_concurrent_bounds() -> None:
    """Out-of-range max_concurrent raises ConfigError."""
    with pytest.raises(ConfigError, match="between 1 and 32"):
        _parse_app_settings({"max_concurrent": 0})
    with pytest.raises(ConfigError, match="between 1 and 32"):
        _parse_app_settings({"max_concurrent": 33})


def test_parse_app_settings_max_concurrent_bad_type() -> None:
    """Non-integer max_concurrent raises ConfigError."""
    with pytest.raises(ConfigError, match="must be an integer"):
        _parse_app_settings({"max_concurrent": "fast"})


def test_parse_app_settings_max_concurrent_bool_rejected() -> None:
    """Boolean is rejected even though bool is subclass of int."""
    with pytest.raises(ConfigError, match="must be an integer"):
        _parse_app_settings({"max_concurrent": True})
