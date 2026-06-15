# Extra settings loaded by NEMO via EXTRA_SETTINGS_FILE env var.
# Appends our plugin to whatever INSTALLED_APPS NEMO already has.
from NEMO.settings import INSTALLED_APPS  # noqa: F401

INSTALLED_APPS = list(INSTALLED_APPS) + ["labfiles_plugin.apps.LabFilesPluginConfig"]
