import json
import logging
import subprocess

logger = logging.getLogger(__name__)


class SambaController:
    """
    Inspects Samba state via `smbstatus`. Used only for inspecting state,
    not for driving Samba — bind mounts are native kernel-level directory
    projections, so `smbcontrol smbd reload-config` is never needed for
    mount/unmount operations.
    """

    def __init__(self, status_command: str = "smbstatus", dry_run: bool = False):
        self.status_command = status_command
        self.dry_run = dry_run

    def get_open_handles(self, machine_id: str) -> list[dict]:
        """
        Returns open files on the share for `machine_id`, as a list of:
          {"path": str, "mode": "read" | "write"}

        Lets IdleMonitor distinguish files merely open for reading from
        files with an active write handle.
        """
        if self.dry_run:
            logger.info("samba [DRY RUN]: smbstatus -L --json (machine=%s) -> []", machine_id)
            return []

        result = subprocess.run(
            [self.status_command, "-L", "--json"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"smbstatus failed ({result.returncode}): {result.stderr}")

        data = json.loads(result.stdout)
        handles = []
        for entry in data.get("open_files", {}).values():
            if entry.get("service_path", "").rstrip("/").endswith(machine_id) or \
                    entry.get("servicepath", "").rstrip("/").endswith(machine_id):
                handles.append({
                    "path": entry.get("filename", ""),
                    "mode": "write" if self._is_write_handle(entry) else "read",
                })
        return handles

    def get_connected_clients(self, machine_id: str) -> list[str]:
        """
        Returns list of currently connected client IPs on this share.
        Used for audit logging and the /sessions debug endpoint.
        """
        if self.dry_run:
            logger.info("samba [DRY RUN]: smbstatus -p --json (machine=%s) -> []", machine_id)
            return []

        result = subprocess.run(
            [self.status_command, "-p", "--json"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"smbstatus failed ({result.returncode}): {result.stderr}")

        data = json.loads(result.stdout)
        clients = []
        for session in data.get("sessions", {}).values():
            if session.get("share", "") == machine_id or \
                    any(t.get("service", "") == machine_id for t in data.get("tcons", {}).values()
                        if t.get("session_id") == session.get("session_id")):
                ip = session.get("remote_machine") or session.get("ip_addr")
                if ip:
                    clients.append(ip)
        return clients

    @staticmethod
    def _is_write_handle(entry: dict) -> bool:
        """
        An open_files entry from `smbstatus -L --json` has an
        "access_mask" field (SMB access mask bits). A write handle is one
        where any of the write-related bits are set:
          FILE_WRITE_DATA (0x2), FILE_APPEND_DATA (0x4),
          FILE_WRITE_ATTRIBUTES (0x100), DELETE (0x10000)
        """
        write_bits = 0x2 | 0x4 | 0x100 | 0x10000
        access_mask = entry.get("access_mask", 0)
        if isinstance(access_mask, str):
            access_mask = int(access_mask, 16) if access_mask.startswith("0x") else int(access_mask)
        return bool(access_mask & write_bits)
