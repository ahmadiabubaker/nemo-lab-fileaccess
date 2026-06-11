import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class MountManager:
    """
    Manages bind mounts and POSIX ACLs for tool sessions.

    On tool_login:
      - Bind-mounts the user's private dir, lab group dir, and public dir
        into the machine's session directory under /mnt/labsessions/
      - Grants the machine account temporary POSIX ACL access to the
        underlying source directories

    On tool_logout:
      - Unmounts the three bind mounts
      - Strips the machine account's ACL entries
      - Only strips group/public ACLs if this is the user's last active
        session (another machine may still need them)
    """

    def __init__(
        self,
        base_path: str = "/srv/labdata",
        sessions_path: str = "/mnt/labsessions",
        dry_run: bool = False,
    ):
        self.base_path = base_path
        self.sessions_path = sessions_path
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mount(self, user_id: str, machine_id: str, group_id: str, machine_account: str) -> bool:
        """
        Called on tool_login. Runs bind mounts and grants ACLs.
        Returns True on success, False if any step fails.
        """
        logger.info("mount: user=%s machine=%s group=%s", user_id, machine_id, group_id)

        src_user = os.path.join(self.base_path, "users", user_id)
        src_group = os.path.join(self.base_path, "groups", group_id) if group_id else None
        src_public = os.path.join(self.base_path, "public")

        dst_base = os.path.join(self.sessions_path, machine_id)
        dst_user = os.path.join(dst_base, "my_files")
        dst_group = os.path.join(dst_base, "lab_shared")
        dst_public = os.path.join(dst_base, "public")

        try:
            # Ensure source directories exist
            self._ensure_dir(src_user)
            self._ensure_dir(src_public)

            # Ensure bind mount target directories exist
            self._ensure_dir(dst_user)
            self._ensure_dir(dst_public)

            # Bind mount user directory
            self._bind_mount(src_user, dst_user)

            # Bind mount public directory
            self._bind_mount(src_public, dst_public)

            # Group directory is optional — only if user belongs to a group
            if src_group:
                self._ensure_dir(src_group)
                self._ensure_dir(dst_group)
                self._bind_mount(src_group, dst_group)

            # Grant POSIX ACLs on source dirs to the machine account
            self._grant_acl(machine_account, "rwx", src_user)
            self._grant_acl(machine_account, "r-x", src_public)
            if src_group:
                self._grant_acl(machine_account, "r-x", src_group)

        except Exception as e:
            logger.error("mount: FAILED user=%s machine=%s: %s", user_id, machine_id, e)
            return False

        logger.info("mount: complete user=%s machine=%s", user_id, machine_id)
        return True

    def unmount(
        self,
        user_id: str,
        machine_id: str,
        group_id: str,
        machine_account: str,
        remaining_sessions: list[str],
    ) -> bool:
        """
        Called on tool_logout. Removes bind mounts and strips ACLs.
        remaining_sessions: list of other machine_ids this user is still
        logged into. Group/public ACLs are only stripped when this list
        is empty — another active session may still need them.
        Returns True on success, False if any step fails.
        """
        logger.info(
            "unmount: user=%s machine=%s group=%s remaining=%s",
            user_id, machine_id, group_id, remaining_sessions,
        )

        src_user = os.path.join(self.base_path, "users", user_id)
        src_group = os.path.join(self.base_path, "groups", group_id) if group_id else None
        src_public = os.path.join(self.base_path, "public")

        dst_base = os.path.join(self.sessions_path, machine_id)
        dst_user = os.path.join(dst_base, "my_files")
        dst_group = os.path.join(dst_base, "lab_shared")
        dst_public = os.path.join(dst_base, "public")

        try:
            self._unmount(dst_user)
            self._unmount(dst_public)
            if src_group:
                self._unmount(dst_group)

            # Remove the empty mount-point directories so SMB clients see nothing
            self._remove_dir(dst_user)
            self._remove_dir(dst_public)
            if src_group:
                self._remove_dir(dst_group)

            # Always strip this machine account's ACL on the user's private dir
            self._strip_acl(machine_account, src_user)

            # Only strip group/public ACLs if no other sessions are active
            # for this user — another machine may still need them
            if not remaining_sessions:
                self._strip_acl(machine_account, src_public)
                if src_group:
                    self._strip_acl(machine_account, src_group)
            else:
                logger.info(
                    "unmount: keeping group/public ACLs — user=%s still active on %s",
                    user_id, remaining_sessions,
                )

        except Exception as e:
            logger.error("unmount: FAILED user=%s machine=%s: %s", user_id, machine_id, e)
            return False

        logger.info("unmount: complete user=%s machine=%s", user_id, machine_id)
        return True

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _ensure_dir(self, path: str) -> None:
        if self.dry_run:
            logger.info("mount [DRY RUN]: mkdir -p %s", path)
            return
        os.makedirs(path, exist_ok=True)

    def _remove_dir(self, path: str) -> None:
        if self.dry_run:
            logger.info("unmount [DRY RUN]: rmdir %s", path)
            return
        try:
            os.rmdir(path)
            logger.info("unmount: removed dir %s", path)
        except OSError as e:
            logger.warning("unmount: could not remove dir %s: %s", path, e)

    def _bind_mount(self, src: str, dst: str) -> None:
        # If already mounted (leftover from a crashed session), force unmount first
        if not self.dry_run and self._is_mounted(dst):
            logger.warning("mount: %s already mounted — force unmounting stale mount", dst)
            self._run(["umount", "-l", dst])

        self._run(["mount", "--bind", src, dst])
        logger.info("mount: bind %s → %s", src, dst)

    def _unmount(self, path: str) -> None:
        if not self.dry_run and not self._is_mounted(path):
            logger.warning("unmount: %s is not mounted, skipping", path)
            return
        self._run(["umount", "-l", path])
        logger.info("unmount: %s", path)

    def _grant_acl(self, account: str, perms: str, path: str) -> None:
        self._run(["setfacl", "-m", f"u:{account}:{perms}", path])
        logger.info("mount: setfacl -m u:%s:%s %s", account, perms, path)

    def _strip_acl(self, account: str, path: str) -> None:
        self._run(["setfacl", "-x", f"u:{account}", path])
        logger.info("unmount: setfacl -x u:%s %s", account, path)

    def _is_mounted(self, path: str) -> bool:
        result = subprocess.run(
            ["mountpoint", "-q", path],
            capture_output=True,
        )
        return result.returncode == 0

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """
        Never uses shell=True — all arguments passed as a list to prevent
        injection via crafted user_id or machine_id values.
        """
        if self.dry_run:
            logger.info("mount [DRY RUN]: %s", " ".join(cmd))
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

        logger.debug("mount: running %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr}"
            )
        return result
