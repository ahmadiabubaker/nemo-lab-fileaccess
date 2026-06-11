from django.apps import AppConfig


class LabFilesPluginConfig(AppConfig):
    name = "labfiles_plugin"
    verbose_name = "Lab Files Plugin"

    def ready(self):
        from labfiles_plugin.signals import connect_signals
        connect_signals()
