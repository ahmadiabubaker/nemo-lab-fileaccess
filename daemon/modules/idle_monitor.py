import logging
import threading
import time

logger = logging.getLogger(__name__)


class IdleMonitor:
    """
    Runs the graceful tool_logout unmount sequence in a background thread,
    so the HTTP response to /unmount returns immediately.

    - Never force-unmounts a directory while a file inside it has an open
      WRITE handle — could corrupt in-progress instrument saves.
    - Read-only handles are subject to max_idle_timeout_seconds, after which
      the mount is force-unmounted regardless, with a warning logged.
    - Ghost session check: if the user re-logs into the same tool while this
      wait is in progress, StateDB.open_session() flips the session status
      back to 'active'. This is detected immediately before unmounting, and
      the unmount is silently aborted.
    """

    def __init__(self, samba_controller, mount_manager, session_manager, state_db,
                 check_interval_seconds: int = 5, max_idle_timeout_seconds: int = 30,
                 audit_logger=None):
        self.samba_controller = samba_controller
        self.mount_manager = mount_manager
        self.session_manager = session_manager
        self.state_db = state_db
        self.check_interval_seconds = check_interval_seconds
        self.max_idle_timeout_seconds = max_idle_timeout_seconds
        self.audit_logger = audit_logger

    def start_unmount(self, user_id: int, machine_id: str, projects: list[dict],
                      username: str = "") -> None:
        """
        Launches wait_and_unmount() in a background thread. Does not block.

        projects: [{"account_id": int, "project_id": int,
                     "account_name": str, "project_name": str}, ...]
        """
        thread = threading.Thread(
            target=self.wait_and_unmount,
            args=(user_id, machine_id, projects, username),
            daemon=True,
        )
        thread.start()

    def wait_and_unmount(self, user_id: int, machine_id: str, projects: list[dict],
                         username: str = "") -> bool:
        """
        Blocks (in its own thread) until it is safe to unmount, then unmounts.
        Returns True if the unmount completed, False if aborted (ghost session).
        """
        idle_elapsed = 0

        while True:
            handles = self.samba_controller.get_open_handles(machine_id)
            write_handles = [h for h in handles if h["mode"] == "write"]
            read_handles = [h for h in handles if h["mode"] == "read"]

            if not handles:
                break

            if write_handles:
                logger.info(
                    "idle_monitor: user=%s machine=%s waiting — %d active write handle(s)",
                    user_id, machine_id, len(write_handles),
                )
                idle_elapsed = 0  # reset idle clock while writes are in progress
            elif read_handles:
                if idle_elapsed >= self.max_idle_timeout_seconds:
                    logger.warning(
                        "idle_monitor: user=%s machine=%s force-unmounting after %ds idle "
                        "with %d read-only handle(s) still open",
                        user_id, machine_id, idle_elapsed, len(read_handles),
                    )
                    break
                idle_elapsed += self.check_interval_seconds

            time.sleep(self.check_interval_seconds)

        # Ghost session check: re-query StateDB immediately before unmounting.
        # If the user re-logged into this tool during the wait, status flips
        # back to 'active' — abort cleanly without unmounting or stripping ACLs.
        session = self.state_db.get_session(user_id, machine_id)
        if session is None or session["status"] != "unmounting":
            status = session["status"] if session else "missing"
            logger.info(
                "idle_monitor: user=%s machine=%s aborting unmount — session status is %s "
                "(user re-logged in during wait window)",
                user_id, machine_id, status,
            )
            if self.audit_logger:
                self.audit_logger.log("tool_logout", user_id, machine_id, "unmount", "aborted",
                                       reason=f"session status is {status}")
            return False

        machine_account = f"{machine_id}_machine"

        # Remove this machine from the session list BEFORE computing
        # remaining_sessions, so MountManager correctly sees whether any
        # OTHER machine still needs the project/public ACLs.
        self.session_manager.remove(user_id, machine_id)
        remaining = self.session_manager.get_machines(user_id)

        ok = self.mount_manager.unmount(
            user_id=user_id, username=username, machine_id=machine_id, projects=projects,
            machine_account=machine_account, remaining_sessions=remaining,
        )
        if not ok:
            logger.error("idle_monitor: unmount FAILED user=%s machine=%s", user_id, machine_id)

        self.state_db.close_session(user_id, machine_id)
        logger.info("idle_monitor: completed unmount user=%s machine=%s", user_id, machine_id)

        if self.audit_logger:
            self.audit_logger.log("tool_logout", user_id, machine_id, "unmount",
                                   "success" if ok else "error")
        return True
