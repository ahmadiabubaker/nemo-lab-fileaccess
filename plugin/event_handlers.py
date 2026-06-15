import logging

logger = logging.getLogger(__name__)


class EventHandlers:
    """
    Translates NemoCE plugin events into DaemonClient calls.

    user_id is always NEMO's numeric id (see README Section 6) — the
    daemon uses it directly as the Linux uid and as the directory name
    under users/. Project/group membership is not sent here; NemoSync
    (daemon-side) resolves memberships independently.
    """

    def __init__(self, daemon_client, tool_id_to_machine_id: dict[str, str]):
        self.daemon_client = daemon_client
        self.tool_id_to_machine_id = tool_id_to_machine_id

    def handle_user_created(self, user_id: int, user_data: dict) -> bool:
        username = user_data.get("username", "")
        full_name = user_data.get("full_name", "")
        email = user_data.get("email", "")
        return self.daemon_client.provision(
            user_id=user_id, username=username, full_name=full_name, email=email,
        )

    def handle_tool_login(self, user_id: int, tool_id: str, session_id: str) -> bool:
        machine_id = self._machine_id_for_tool(tool_id)
        if machine_id is None:
            return False
        return self.daemon_client.mount(user_id=user_id, machine_id=machine_id, session_id=session_id)

    def handle_tool_logout(self, user_id: int, tool_id: str, session_id: str) -> bool:
        machine_id = self._machine_id_for_tool(tool_id)
        if machine_id is None:
            return False
        return self.daemon_client.unmount(user_id=user_id, machine_id=machine_id, session_id=session_id)

    def _machine_id_for_tool(self, tool_id: str) -> str | None:
        machine_id = self.tool_id_to_machine_id.get(tool_id)
        if machine_id is None:
            logger.warning("labfiles: no machine_id mapping configured for tool_id=%s", tool_id)
        return machine_id
