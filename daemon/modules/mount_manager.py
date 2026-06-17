import logging
import os
import subprocess
from collections import defaultdict

logger = logging.getLogger(__name__)


class MountManager:
    """
    Manages bind mounts and POSIX ACLs for tool sessions.

    Structure visible to users under \\server\MACHINE\:
        {username}/          — personal files
        my_groups/
            {Account Name}/
                {Project Name}/
        public/

    Vault paths (never visible):
        /srv/labdata/users/{user_id}/
        /srv/labdata/groups/account_{account_id}/project_{project_id}/
        /srv/labdata/public/

    On tool_login:
      - Creates the above structure as bind mounts under /mnt/labsessions/{machine_id}/
      - Grants machine account POSIX ACL access to underlying source directories

    On tool_logout:
      - Unmounts and removes all bind-mount target directories
      - Strips machine account ACL entries
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

    def mount(self, user_id: int, username: str, machine_id: str,
              projects: list[dict], machine_account: str) -> bool:
        """
        Called on tool_login.

        projects: [{"account_id": int, "project_id": int,
                     "account_name": str, "project_name": str}, ...]
        """
        logger.info("mount: user=%s (%s) machine=%s projects=%s",
                    user_id, username, machine_id, [p["project_id"] for p in projects])

        src_user = os.path.join(self.base_path, "users", str(user_id))
        src_public = os.path.join(self.base_path, "public")

        dst_base = os.path.join(self.sessions_path, machine_id)
        dst_user = os.path.join(dst_base, username)
        dst_groups = os.path.join(dst_base, "my_groups")
        dst_public = os.path.join(dst_base, "public")

        try:
            self._ensure_dir(src_user)
            self._ensure_dir(src_public)

            # Personal folder (named after the user)
            self._ensure_dir(dst_user)
            self._bind_mount(src_user, dst_user)
            self._grant_acl(machine_account, "rwx", src_user)

            # Public folder
            self._ensure_dir(dst_public)
            self._bind_mount(src_public, dst_public)
            self._grant_acl(machine_account, "r-x", src_public)

            # my_groups/{Account Name}/{Project Name}/
            # Group projects by account so we create one account folder each
            by_account: dict[int, list[dict]] = defaultdict(list)
            for p in projects:
                by_account[p["account_id"]].append(p)

            for account_projects in by_account.values():
                account_name = account_projects[0]["account_name"]
                dst_account = os.path.join(dst_groups, account_name)
                self._ensure_dir(dst_account)

                for p in account_projects:
                    src_project = self._project_path(p["account_id"], p["project_id"])
                    dst_project = os.path.join(dst_account, p["project_name"])

                    self._ensure_dir(dst_project)
                    self._bind_mount(src_project, dst_project)
                    self._grant_acl(machine_account, "rwx", src_project)

        except Exception as e:
            logger.error("mount: FAILED user=%s machine=%s: %s", user_id, machine_id, e)
            return False

        logger.info("mount: complete user=%s machine=%s", user_id, machine_id)
        return True

    def unmount(
        self,
        user_id: int,
        username: str,
        machine_id: str,
        projects: list[dict],
        machine_account: str,
        remaining_sessions: list[str],
    ) -> bool:
        """
        Called on tool_logout.

        projects: [{"account_id": int, "project_id": int,
                     "account_name": str, "project_name": str}, ...]
        remaining_sessions: other machine_ids this user is still logged into.
        """
        logger.info(
            "unmount: user=%s machine=%s projects=%s remaining=%s",
            user_id, machine_id, [p["project_id"] for p in projects], remaining_sessions,
        )

        src_user = os.path.join(self.base_path, "users", str(user_id))
        src_public = os.path.join(self.base_path, "public")

        dst_base = os.path.join(self.sessions_path, machine_id)
        dst_user = os.path.join(dst_base, username)
        dst_groups = os.path.join(dst_base, "my_groups")
        dst_public = os.path.join(dst_base, "public")

        try:
            self._unmount(dst_user)
            self._remove_dir(dst_user)

            self._unmount(dst_public)
            self._remove_dir(dst_public)

            by_account: dict[int, list[dict]] = defaultdict(list)
            for p in projects:
                by_account[p["account_id"]].append(p)

            for account_projects in by_account.values():
                account_name = account_projects[0]["account_name"]
                dst_account = os.path.join(dst_groups, account_name)

                for p in account_projects:
                    dst_project = os.path.join(dst_account, p["project_name"])
                    self._unmount(dst_project)
                    self._remove_dir(dst_project)

                self._remove_dir(dst_account)

            self._remove_dir(dst_groups)

            self._strip_acl(machine_account, src_user)

            if not remaining_sessions:
                self._strip_acl(machine_account, src_public)
                for p in projects:
                    src_project = self._project_path(p["account_id"], p["project_id"])
                    self._strip_acl(machine_account, src_project)
            else:
                logger.info(
                    "unmount: keeping project/public ACLs — user=%s still active on %s",
                    user_id, remaining_sessions,
                )

        except Exception as e:
            logger.error("unmount: FAILED user=%s machine=%s: %s", user_id, machine_id, e)
            return False

        logger.info("unmount: complete user=%s machine=%s", user_id, machine_id)
        return True

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _project_path(self, account_id: int, project_id: int) -> str:
        return os.path.join(self.base_path, "groups",
                            f"account_{account_id}", f"project_{project_id}")

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
