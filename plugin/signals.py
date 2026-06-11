import os
import logging
import requests
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

DAEMON_URL = os.environ.get("LABFILES_DAEMON_URL", "http://fileserver-daemon:5000")
API_KEY = os.environ.get("LABFILES_API_KEY", "dev_key")

_session = requests.Session()
_session.headers["X-API-Key"] = API_KEY
_session.timeout = 10


def _post(endpoint, payload):
    try:
        resp = _session.post(f"{DAEMON_URL}/{endpoint}", json=payload)
        resp.raise_for_status()
        logger.info("labfiles: %s -> %s %s", endpoint, resp.status_code, payload)
    except Exception as e:
        logger.error("labfiles: failed to POST /%s: %s", endpoint, e)


def connect_signals():
    User = get_user_model()

    @receiver(post_save, sender=User)
    def on_user_saved(sender, instance, created, **kwargs):
        if not created:
            return
        user_id = instance.username
        project = instance.projects.first() if hasattr(instance, "projects") else None
        group_id = project.name if project else ""
        full_name = instance.get_full_name()
        _post("provision", {
            "event": "user_created",
            "user_id": user_id,
            "group_id": group_id,
            "full_name": full_name,
        })


@receiver(post_save, sender="NEMO.UsageEvent")
def on_usage_event_saved(sender, instance, created, **kwargs):
    user_id = instance.user.username
    # tool.name is the machine_id — Dan maps tool names to machine IDs in config
    machine_id = instance.tool.name
    session_id = str(instance.pk)

    if created:
        # A new UsageEvent with no end time = tool login
        _post("mount", {
            "event": "tool_login",
            "user_id": user_id,
            "machine_id": machine_id,
            "session_id": session_id,
        })
    elif instance.end is not None:
        # An existing UsageEvent just got an end time = tool logout
        _post("unmount", {
            "event": "tool_logout",
            "user_id": user_id,
            "machine_id": machine_id,
            "session_id": session_id,
        })
