import os
import yaml

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "plugin_config.yaml")

DEFAULTS = {
    "daemon": {
        "base_url": "https://fileserver-daemon:5443",
        "api_key": "CHANGE_THIS_IN_PRODUCTION",
        "client_cert": "",
        "client_key": "",
        "ca_cert": "",
        "timeout_seconds": 10,
    },
    "tools": [],
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_plugin_config(config_path: str | None = None) -> dict:
    """
    Loads plugin_config.yaml (if present) and merges it over DEFAULTS.

    `tools` is a list of {tool_id, machine_id, samba_user} entries used to
    map NemoCE's internal tool_id to this project's machine_id.
    """
    config_path = config_path or os.environ.get("LABFILES_PLUGIN_CONFIG_PATH", DEFAULT_CONFIG_PATH)

    config = dict(DEFAULTS)
    if os.path.isfile(config_path):
        with open(config_path) as f:
            file_config = yaml.safe_load(f) or {}
        config = _deep_merge(config, file_config)

    return config


def tool_id_to_machine_id(config: dict) -> dict[str, str]:
    return {t["tool_id"]: t["machine_id"] for t in config.get("tools", [])}
