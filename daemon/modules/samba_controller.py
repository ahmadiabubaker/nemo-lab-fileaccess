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

    Supports both Samba 4.17+ (--json) and older versions (plain-text parsing).
    """

    def __init__(self, status_command: str = "smbstatus", dry_run: bool = False):
        self.status_command = status_command
        self.dry_run = dry_run
        self._use_json = None  # detected on first call

    def _json_supported(self) -> bool:
        if self._use_json is None:
            result = subprocess.run(
                [self.status_command, "--json", "--version"],
                capture_output=True, text=True,
            )
            self._use_json = result.returncode == 0
            if not self._use_json:
                logger.info("samba: --json not supported, using plain-text smbstatus parsing")
        return self._use_json

    def get_open_handles(self, machine_id: str) -> list[dict]:
        """
        Returns open files on the share for `machine_id`, as a list of:
          {"path": str, "mode": "read" | "write"}
        """
        if self.dry_run:
            logger.info("samba [DRY RUN]: smbstatus -L (machine=%s) -> []", machine_id)
            return []

        if self._json_supported():
            return self._get_open_handles_json(machine_id)
        return self._get_open_handles_text(machine_id)

    def _get_open_handles_json(self, machine_id: str) -> list[dict]:
        result = subprocess.run(
            [self.status_command, "-L", "--json"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"smbstatus failed ({result.returncode}): {result.stderr}")

        data = json.loads(result.stdout)
        handles = []
        for entry in data.get("open_files", {}).values():
            share_path = entry.get("service_path", "") or entry.get("servicepath", "")
            if share_path.rstrip("/").endswith(machine_id):
                handles.append({
                    "path": entry.get("filename", ""),
                    "mode": "write" if self._is_write_handle(entry) else "read",
                })
        return handles

    def _get_open_handles_text(self, machine_id: str) -> list[dict]:
        result = subprocess.run(
            [self.status_command, "-L"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"smbstatus failed ({result.returncode}): {result.stderr}")

        handles = []
        in_locked = False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                in_locked = False
                continue
            if stripped.startswith("Locked files:"):
                in_locked = True
                continue
            if stripped.startswith("Pid") and "SharePath" in stripped:
                continue  # header row
            if not in_locked:
                continue

            # Columns: Pid Uid DenyMode Access R/W Oplock SharePath Name Time
            parts = stripped.split()
            if len(parts) < 8:
                continue
            # SharePath is 7th column (index 6), Name is 8th (index 7)
            share_path = parts[6]
            filename = parts[7] if len(parts) > 7 else ""
            rw = parts[4].upper()  # "RDONLY", "WRONLY", "RDWR"

            if share_path.rstrip("/").endswith(machine_id):
                mode = "write" if rw in ("WRONLY", "RDWR") else "read"
                handles.append({"path": filename, "mode": mode})
        return handles

    def get_connected_clients(self, machine_id: str) -> list[str]:
        """Returns list of currently connected client IPs on this share."""
        if self.dry_run:
            logger.info("samba [DRY RUN]: smbstatus -p (machine=%s) -> []", machine_id)
            return []

        if self._json_supported():
            return self._get_connected_clients_json(machine_id)
        return self._get_connected_clients_text(machine_id)

    def _get_connected_clients_json(self, machine_id: str) -> list[str]:
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

    def _get_connected_clients_text(self, machine_id: str) -> list[str]:
        result = subprocess.run(
            [self.status_command, "-p"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"smbstatus failed ({result.returncode}): {result.stderr}")

        # Plain smbstatus -p doesn't filter by share — return all connected IPs
        # as a conservative over-approximation (keeps sessions alive longer, safe).
        clients = []
        in_sessions = False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                in_sessions = False
                continue
            if "Samba version" in stripped or stripped.startswith("PID") and "Username" in stripped:
                in_sessions = True
                continue
            if not in_sessions:
                continue
            parts = stripped.split()
            # Columns: PID Username Group Machine (proto) IP address
            # IP is typically last or second-to-last
            for part in reversed(parts):
                if part.count(".") == 3 or ":" in part:  # IPv4 or IPv6
                    clients.append(part)
                    break
        return clients

    @staticmethod
    def _is_write_handle(entry: dict) -> bool:
        write_bits = 0x2 | 0x4 | 0x100 | 0x10000
        access_mask = entry.get("access_mask", 0)
        if isinstance(access_mask, str):
            access_mask = int(access_mask, 16) if access_mask.startswith("0x") else int(access_mask)
        return bool(access_mask & write_bits)
