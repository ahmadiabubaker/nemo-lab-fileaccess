import logging

from .plugin_config import load_plugin_config, tool_id_to_machine_id
from .http_client import DaemonClient
from .event_handlers import EventHandlers

logger = logging.getLogger(__name__)


class LabFilesPlugin:
    """
    NemoCE plugin for the lab file access system.

    Sends HTTPS (mTLS) events to the file server daemon on user lifecycle
    events: new user creation, tool login, and tool logout. See README
    Section 9 for the NemoCE plugin specification and Section 6 for the
    payload shapes expected by the daemon.
    """

    def __init__(self):
        config = load_plugin_config()
        daemon_config = config["daemon"]

        client_cert = (daemon_config["client_cert"], daemon_config["client_key"])
        daemon_client = DaemonClient(
            base_url=daemon_config["base_url"],
            api_key=daemon_config["api_key"],
            client_cert=client_cert,
            ca_cert=daemon_config["ca_cert"] or None,
            timeout_seconds=daemon_config["timeout_seconds"],
        )

        self.handlers = EventHandlers(
            daemon_client=daemon_client,
            tool_id_to_machine_id=tool_id_to_machine_id(config),
        )

    def on_user_created(self, user_id: int, user_data: dict) -> None:
        """Fires when a new user is added to Nemo. POSTs to /provision."""
        self.handlers.handle_user_created(user_id, user_data)

    def on_tool_login(self, user_id: int, tool_id: str, session_id: str) -> None:
        """Fires when a user selects and logs into a tool. POSTs to /mount."""
        self.handlers.handle_tool_login(user_id, tool_id, session_id)

    def on_tool_logout(self, user_id: int, tool_id: str, session_id: str) -> None:
        """Fires when a user releases a tool. POSTs to /unmount."""
        self.handlers.handle_tool_logout(user_id, tool_id, session_id)
