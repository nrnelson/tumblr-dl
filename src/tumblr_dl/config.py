"""Configuration loading for tumblr-dl.

Supports environment variables, .env files, and TOML config files.
Auth priority: env vars > TOML [auth] section.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tumblr_dl.exceptions import ConfigError

logger = logging.getLogger(__name__)

_AUTH_ENV_VARS = {
    "consumer_key": "TUMBLR_CONSUMER_KEY",
    "consumer_secret": "TUMBLR_CONSUMER_SECRET",
    "oauth_token": "TUMBLR_OAUTH_TOKEN",
    "oauth_token_secret": "TUMBLR_OAUTH_TOKEN_SECRET",
}


@dataclass(frozen=True)
class AuthCredentials:
    """OAuth1 credentials for Tumblr API access."""

    consumer_key: str
    consumer_secret: str
    oauth_token: str
    oauth_token_secret: str


@dataclass
class BlogConfig:
    """Configuration for a single blog download job."""

    output_dir: str = "tumblr_downloads"
    exclude_tags: list[str] = field(default_factory=list)
    exclude_blogs: list[str] = field(default_factory=list)
    max_posts: int | None = None
    start_post: int = 0
    tag: str | None = None
    full_scan: bool = False
    retry_failed: bool = False
    no_db: bool = False
    db_path: str | None = None


@dataclass
class AppSettings:
    """App-level settings from the TOML ``[settings]`` section."""

    debug: bool = False
    log_file: str | None = None
    max_concurrent: int = 4


@dataclass
class AppConfig:
    """Top-level application configuration from TOML."""

    auth: AuthCredentials | None = None
    settings: AppSettings = field(default_factory=AppSettings)
    defaults: BlogConfig = field(default_factory=BlogConfig)
    blogs: dict[str, BlogConfig] = field(default_factory=dict)


def _platform_config_dir() -> Path:
    """Return the platform-appropriate config directory for tumblr-dl.

    Resolution order:
    1. ``$XDG_CONFIG_HOME/tumblr-dl`` (if set, any platform)
    2. ``%APPDATA%/tumblr-dl`` (Windows default)
    3. ``~/.config/tumblr-dl`` (Unix/macOS default)
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        return Path(xdg) / "tumblr-dl"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "tumblr-dl"
    return Path.home() / ".config" / "tumblr-dl"


def resolve_config_path() -> Path | None:
    """Find the TOML config file in the platform config directory.

    Returns:
        Path to config.toml if it exists, None otherwise.
    """
    config_file = _platform_config_dir() / "config.toml"
    if config_file.is_file():
        return config_file
    return None


def _load_auth_from_env() -> AuthCredentials | None:
    """Try loading all four OAuth keys from environment variables.

    Returns:
        AuthCredentials if all four are set, None otherwise.

    Raises:
        ConfigError: If some but not all env vars are set.
    """
    found = {key: os.environ.get(env_var) for key, env_var in _AUTH_ENV_VARS.items()}
    present = {k for k, v in found.items() if v}
    if not present:
        return None
    if len(present) < 4:
        missing = [_AUTH_ENV_VARS[k] for k in _AUTH_ENV_VARS if k not in present]
        raise ConfigError(
            f"Partial OAuth env vars set; missing: {', '.join(missing)}",
            context={"present": sorted(present), "missing": missing},
        )
    return AuthCredentials(
        consumer_key=found["consumer_key"],  # type: ignore[arg-type]
        consumer_secret=found["consumer_secret"],  # type: ignore[arg-type]
        oauth_token=found["oauth_token"],  # type: ignore[arg-type]
        oauth_token_secret=found["oauth_token_secret"],  # type: ignore[arg-type]
    )


def _parse_blog_config(
    data: dict[str, object], section_name: str = "blog"
) -> BlogConfig:
    """Parse a TOML table into a BlogConfig."""
    config = BlogConfig()
    str_fields = {"output_dir", "tag", "db_path"}
    list_fields = {"exclude_tags", "exclude_blogs"}
    int_fields = {"max_posts", "start_post"}
    bool_fields = {"full_scan", "retry_failed", "no_db"}
    known_keys = str_fields | list_fields | int_fields | bool_fields

    for key, value in data.items():
        if key not in known_keys:
            logger.warning(
                "Unknown config key '%s' in [%s] section (ignored).",
                key,
                section_name,
            )
            continue
        if key in str_fields:
            if not isinstance(value, str):
                raise ConfigError(
                    f"Config key '{key}' must be a string, got {type(value).__name__}",
                    context={"key": key, "value": value},
                )
            setattr(config, key, value)
        elif key in list_fields:
            if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value
            ):
                raise ConfigError(
                    f"Config key '{key}' must be a list of strings",
                    context={"key": key, "value": value},
                )
            setattr(config, key, value)
        elif key in int_fields:
            if not isinstance(value, int):
                raise ConfigError(
                    f"Config key '{key}' must be an integer, "
                    f"got {type(value).__name__}",
                    context={"key": key, "value": value},
                )
            setattr(config, key, value)
        elif key in bool_fields:
            if not isinstance(value, bool):
                raise ConfigError(
                    f"Config key '{key}' must be a boolean, got {type(value).__name__}",
                    context={"key": key, "value": value},
                )
            setattr(config, key, value)

    return config


def _parse_app_settings(data: dict[str, object]) -> AppSettings:
    """Parse a TOML ``[settings]`` table into an AppSettings."""
    settings = AppSettings()
    for key, value in data.items():
        if key == "debug":
            if not isinstance(value, bool):
                raise ConfigError(
                    f"Config key 'settings.{key}' must be a boolean, "
                    f"got {type(value).__name__}",
                    context={"key": key, "value": value},
                )
            settings.debug = value
        elif key == "log_file":
            if not isinstance(value, str):
                raise ConfigError(
                    f"Config key 'settings.{key}' must be a string, "
                    f"got {type(value).__name__}",
                    context={"key": key, "value": value},
                )
            settings.log_file = value
        elif key == "max_concurrent":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(
                    f"Config key 'settings.{key}' must be an integer, "
                    f"got {type(value).__name__}",
                    context={"key": key, "value": value},
                )
            if value < 1 or value > 32:
                raise ConfigError(
                    f"Config key 'settings.{key}' must be between 1 and 32, "
                    f"got {value}",
                    context={"key": key, "value": value},
                )
            settings.max_concurrent = value
        else:
            logger.warning(
                "Unknown config key '%s' in [settings] section (ignored).",
                key,
            )
    return settings


def load_toml_config(path: Path) -> AppConfig:
    """Parse and validate a TOML config file.

    Args:
        path: Path to the TOML file.

    Returns:
        Parsed AppConfig.

    Raises:
        ConfigError: If the file is missing or invalid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Config file not found: {path}",
            context={"path": str(path)},
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Invalid TOML in config: {path}",
            context={"path": str(path), "detail": str(exc)},
        ) from exc

    app = AppConfig()

    # Parse [auth] section.
    auth_data = data.get("auth")
    if isinstance(auth_data, dict):
        required = {
            "consumer_key",
            "consumer_secret",
            "oauth_token",
            "oauth_token_secret",
        }
        present = required & auth_data.keys()
        if present and present != required:
            missing = sorted(required - present)
            raise ConfigError(
                f"Incomplete [auth] section; missing: {', '.join(missing)}",
                context={"missing": missing},
            )
        if present == required:
            app.auth = AuthCredentials(
                consumer_key=str(auth_data["consumer_key"]),
                consumer_secret=str(auth_data["consumer_secret"]),
                oauth_token=str(auth_data["oauth_token"]),
                oauth_token_secret=str(auth_data["oauth_token_secret"]),
            )

    # Parse [options] section — merged app settings, blog defaults, and blog list.
    options_data = data.get("options")
    if isinstance(options_data, dict):
        # Split keys: app-level settings vs blog-level defaults.
        settings_keys = {"debug", "log_file", "max_concurrent"}
        blog_keys = set(vars(BlogConfig()))
        settings_part: dict[str, object] = {}
        defaults_part: dict[str, object] = {}

        for key, value in options_data.items():
            if key == "blogs":
                continue  # handled below
            elif key in settings_keys:
                settings_part[key] = value
            elif key in blog_keys:
                defaults_part[key] = value
            else:
                logger.warning(
                    "Unknown config key '%s' in [options] section (ignored).",
                    key,
                )

        if settings_part:
            app.settings = _parse_app_settings(settings_part)
        if defaults_part:
            app.defaults = _parse_blog_config(defaults_part, section_name="options")

        # Parse blogs array within [options].
        blogs_array = options_data.get("blogs")
        if blogs_array is not None:
            if not isinstance(blogs_array, list) or not all(
                isinstance(b, str) for b in blogs_array
            ):
                raise ConfigError(
                    "Config key 'options.blogs' must be a list of strings",
                    context={"value": blogs_array},
                )
            for name in blogs_array:
                app.blogs[name] = BlogConfig()

    # Parse [blog.*] sections — per-blog overrides (also registers the blog).
    blog_data = data.get("blog")
    if isinstance(blog_data, dict):
        for name, section in blog_data.items():
            if not isinstance(section, dict):
                raise ConfigError(
                    f"[blog.{name}] must be a table",
                    context={"blog": name},
                )
            app.blogs[name] = _parse_blog_config(section, section_name=f"blog.{name}")

    return app


def load_auth(app_config: AppConfig | None = None) -> AuthCredentials:
    """Resolve auth credentials with priority: env vars > TOML [auth].

    Args:
        app_config: Parsed TOML config (may contain [auth] section).

    Returns:
        Resolved AuthCredentials.

    Raises:
        ConfigError: If no auth source provides all four keys.
    """
    # Priority 1: environment variables.
    env_auth = _load_auth_from_env()
    if env_auth:
        logger.debug("Loaded OAuth credentials from environment variables")
        return env_auth

    # Priority 2: TOML [auth] section.
    if app_config and app_config.auth:
        logger.debug("Loaded OAuth credentials from TOML [auth] section")
        return app_config.auth

    raise ConfigError(
        "No OAuth credentials found. Set TUMBLR_CONSUMER_KEY/TUMBLR_CONSUMER_SECRET/"
        "TUMBLR_OAUTH_TOKEN/TUMBLR_OAUTH_TOKEN_SECRET environment variables, "
        "or add an [auth] section to your config.toml.",
        context={"sources_checked": ["environment", "config.toml"]},
    )


def resolve_blog_config(
    blog_name: str | None,
    app_config: AppConfig | None,
    cli_overrides: dict[str, object],
) -> BlogConfig:
    """Merge config layers: hardcoded defaults < TOML defaults < per-blog < CLI.

    Args:
        blog_name: Blog being downloaded, or None for tag-only mode.
        app_config: Parsed TOML config (or None).
        cli_overrides: CLI flag values (None means not provided).

    Returns:
        Fully resolved BlogConfig.
    """
    config = BlogConfig()

    # Layer 2: TOML [defaults].
    if app_config:
        _overlay(config, app_config.defaults)

    # Layer 3: TOML [blog.<name>].
    if app_config and blog_name and blog_name in app_config.blogs:
        _overlay(config, app_config.blogs[blog_name])

    # Layer 4: CLI overrides (only non-None values).
    for key, value in cli_overrides.items():
        if value is not None and hasattr(config, key):
            setattr(config, key, value)

    return config


def _overlay(target: BlogConfig, source: BlogConfig) -> None:
    """Copy non-default values from source onto target.

    Note: values that match BlogConfig() defaults are not copied, so a
    per-blog section cannot reset a [defaults] value back to the default.
    """
    defaults = BlogConfig()
    for attr in vars(defaults):
        source_val = getattr(source, attr)
        if source_val != getattr(defaults, attr):
            setattr(target, attr, source_val)
