import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from NEMO.models import User, UsageEvent

from .plugin_config import load_plugin_config, tool_id_to_machine_id
from .http_client import DaemonClient

logger = logging.getLogger(__name__)

_config = load_plugin_config()
_daemon_config = _config["daemon"]

_client_cert = (_daemon_config["client_cert"], _daemon_config["client_key"])
_daemon_client = DaemonClient(
    base_url=_daemon_config["base_url"],
    api_key=_daemon_config["api_key"],
    client_cert=_client_cert,
    ca_cert=_daemon_config["ca_cert"] or None,
    timeout_seconds=_daemon_config["timeout_seconds"],
)

_tool_id_to_machine_id = tool_id_to_machine_id(_config)


@receiver(post_save, sender=User)
def on_user_created(sender, instance: User, created: bool, **kwargs) -> None:
    if not created:
        return
    _daemon_client.provision(
        user_id=instance.id,
        username=instance.username,
        full_name=f"{instance.first_name} {instance.last_name}".strip(),
        email=instance.email,
    )


@receiver(post_save, sender=UsageEvent)
def on_usage_event_saved(sender, instance: UsageEvent, created: bool, **kwargs) -> None:
    machine_id = _tool_id_to_machine_id.get(str(instance.tool_id))
    if machine_id is None:
        logger.warning("labfiles: no machine_id mapping configured for tool_id=%s", instance.tool_id)
        return

    session_id = str(instance.pk)

    if created and instance.end is None:
        _daemon_client.mount(user_id=instance.user_id, machine_id=machine_id, session_id=session_id)
    elif not created and instance.end is not None:
        _daemon_client.unmount(user_id=instance.user_id, machine_id=machine_id, session_id=session_id)
