import os
import yaml

DEFAULT_CONFIG_PATH = os.path.join("config", "config.yaml")

# Maps LABFILES_* env vars to their location in the config tree, as a
# tuple of nested keys. Env vars take priority over config.yaml values,
# so a single setting can be overridden for local/dev runs without
# editing the file.
_ENV_OVERRIDES = {
    "LABFILES_HOST": ("server", "host"),
    "LABFILES_PORT": ("server", "port"),
    "LABFILES_API_KEY_HASH": ("server", "api_key_hash"),
    "LABFILES_BASE_PATH": ("storage", "base_path"),
    "LABFILES_QUOTA_SOFT_MB": ("storage", "quota_soft_mb"),
    "LABFILES_QUOTA_HARD_MB": ("storage", "quota_hard_mb"),
    "LABFILES_UID_OFFSET": ("storage", "uid_offset"),
    "LABFILES_SESSIONS_PATH": ("sessions", "mount_base_path"),
    "LABFILES_DB_PATH": ("sessions", "db_path"),
    "LABFILES_SAMBA_STATUS_COMMAND": ("samba", "status_command"),
    "LABFILES_IDLE_CHECK_INTERVAL_SECONDS": ("idle_monitor", "check_interval_seconds"),
    "LABFILES_IDLE_MAX_TIMEOUT_SECONDS": ("idle_monitor", "max_idle_timeout_seconds"),
    "LABFILES_NEMO_API_BASE_URL": ("nemo_sync", "api_base_url"),
    "LABFILES_NEMO_API_TOKEN": ("nemo_sync", "api_token"),
    "LABFILES_NEMO_SYNC_POLL_INTERVAL_SECONDS": ("nemo_sync", "poll_interval_seconds"),
    "LABFILES_NEMO_SYNC_ON_DEACTIVATION": ("nemo_sync", "on_deactivation"),
    "LABFILES_AUDIT_LOG_PATH": ("logging", "log_path"),
    "LABFILES_AUDIT_LOG_ROTATION_DAYS": ("logging", "rotation_days"),
}

# Env vars whose values should be coerced to int before being placed in the config tree.
_INT_KEYS = {
    "LABFILES_PORT",
    "LABFILES_QUOTA_SOFT_MB",
    "LABFILES_QUOTA_HARD_MB",
    "LABFILES_UID_OFFSET",
    "LABFILES_IDLE_CHECK_INTERVAL_SECONDS",
    "LABFILES_IDLE_MAX_TIMEOUT_SECONDS",
    "LABFILES_NEMO_SYNC_POLL_INTERVAL_SECONDS",
    "LABFILES_AUDIT_LOG_ROTATION_DAYS",
}

DEFAULTS = {
    "server": {
        "host": "127.0.0.1",
        "port": 5443,
        "api_key_hash": "",
        "tls": {
            "cert_file": "",
            "key_file": "",
            "client_ca_file": "",
            "require_client_cert": False,
        },
    },
    "rate_limiting": {
        "default": "60/minute",
        "mount_endpoints": "20/minute",
    },
    "storage": {
        "base_path": "/srv/labdata",
        "quota_soft_mb": 10240,
        "quota_hard_mb": 12288,
        "uid_offset": 0,
    },
    "sessions": {
        "mount_base_path": "/mnt/labsessions",
        "db_path": "/var/lib/labfiles/sessions.db",
    },
    "samba": {
        "status_command": "smbstatus",
    },
    "idle_monitor": {
        "check_interval_seconds": 5,
        "max_idle_timeout_seconds": 30,
    },
    "nemo_sync": {
        "api_base_url": "",
        "api_token": "",
        "poll_interval_seconds": 3600,
        "on_deactivation": "lock_account",
    },
    "logging": {
        "log_path": "/var/log/labfiles/daemon.log",
        "level": "INFO",
        "rotation_days": 30,
    },
    "machines": [],
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | None = None) -> dict:
    """
    Loads config/config.yaml (if present), merges it over DEFAULTS, then
    applies any LABFILES_* environment variable overrides on top.

    Missing file is not an error — DEFAULTS plus env vars is a valid
    (if minimal) configuration for local/dev runs.
    """
    config_path = config_path or os.environ.get("LABFILES_CONFIG_PATH", DEFAULT_CONFIG_PATH)

    config = dict(DEFAULTS)
    if os.path.isfile(config_path):
        with open(config_path) as f:
            file_config = yaml.safe_load(f) or {}
        config = _deep_merge(config, file_config)

    for env_var, path in _ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is None:
            continue
        if env_var in _INT_KEYS:
            value = int(value)
        node = config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value

    if os.environ.get("LABFILES_MACHINES"):
        config["machines"] = [
            {"id": m.strip()} for m in os.environ["LABFILES_MACHINES"].split(",") if m.strip()
        ]

    return config


def machine_ids(config: dict) -> list[str]:
    return [m["id"] for m in config.get("machines", [])]
