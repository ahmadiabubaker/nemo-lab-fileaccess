from NEMO.plugins.utils import NEMOPluginConfig


class LabFilesPluginConfig(NEMOPluginConfig):
    name = "labfiles_plugin"
    verbose_name = "Lab Files Plugin"
    plugin_id = "labfiles_plugin"

    def ready(self):
        super().ready()
        from . import signals  # noqa: F401
