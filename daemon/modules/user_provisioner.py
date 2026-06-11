import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class UserProvisioner:
    """
    Provisions a new researcher on the file server when NEMO fires user_created.

    Each provision() call is idempotent: calling it twice for the same user
    is safe and produces the same end state.
    """

    def __init__(
        self,
        base_path: str = "/srv/labdata",
        quota_soft_mb: int = 10240,
        quota_hard_mb: int = 12288,
        dry_run: bool = False,
    ):
        self.base_path = base_path
        self.quota_soft_mb = quota_soft_mb
        self.quota_hard_mb = quota_hard_mb
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def provision(self, user_id: str, group_id: str, full_name: str = "") -> bool:
        """
        Full provisioning sequence for a new user:
          1. Create Linux system user (idempotent)
          2. Ensure group exists (idempotent)
          3. Create and permission the user's data directory
          4. Set default ACL so files written by machine accounts stay readable by the user
          5. Add user to their lab group
          6. Set disk quota

        Returns True on success, False if any step fails.
        """
        logger.info("provision: starting user=%s group=%s name=%s", user_id, group_id, full_name)

        required_steps = [
            ("create_linux_user",   lambda: self._create_linux_user(user_id, full_name)),
            ("ensure_group",        lambda: self._ensure_group(group_id)),
            ("create_user_dir",     lambda: self._create_user_dir(user_id)),
            ("set_default_acl",     lambda: self._set_default_acl(user_id)),
            ("add_to_group",        lambda: self._add_to_group(user_id, group_id)),
        ]
        optional_steps = [
            ("set_quota",           lambda: self._set_quota(user_id)),
        ]
        steps = [(name, fn, True) for name, fn in required_steps] + \
                [(name, fn, False) for name, fn in optional_steps]

        for name, step, required in steps:
            try:
                step()
            except Exception as e:
                if required:
                    logger.error("provision: step=%s user=%s FAILED: %s", name, user_id, e)
                    return False
                else:
                    logger.warning("provision: step=%s user=%s skipped: %s", name, user_id, e)

        logger.info("provision: complete user=%s", user_id)
        return True

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _create_linux_user(self, user_id: str, full_name: str) -> None:
        # Check if user already exists — idempotent
        result = self._run(["id", user_id], check=False)
        if result.returncode == 0:
            logger.info("provision: user %s already exists, skipping useradd", user_id)
            return

        cmd = ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin"]
        if full_name:
            cmd += ["--comment", full_name]
        cmd.append(user_id)
        self._run(cmd)
        logger.info("provision: created linux user %s", user_id)

    def _ensure_group(self, group_id: str) -> None:
        if not group_id:
            return
        result = self._run(["getent", "group", group_id], check=False)
        if result.returncode == 0:
            logger.info("provision: group %s already exists", group_id)
            return
        self._run(["groupadd", group_id])
        logger.info("provision: created group %s", group_id)

    def _create_user_dir(self, user_id: str) -> None:
        user_dir = os.path.join(self.base_path, "users", user_id)

        if not os.path.isdir(user_dir):
            self._run(["mkdir", "-p", user_dir])
            logger.info("provision: created directory %s", user_dir)
        else:
            logger.info("provision: directory %s already exists, re-applying permissions", user_dir)

        # Always re-apply ownership and mode (idempotent and safe)
        self._run(["chown", f"{user_id}:{user_id}", user_dir])
        self._run(["chmod", "700", user_dir])

    def _set_default_acl(self, user_id: str) -> None:
        """
        Set a default ACL on the user's directory so that any file written
        by a machine account (e.g. microscope1_machine) automatically inherits
        an ACL entry granting the user full access.

        Without this, a file created by microscope1_machine inside users/harry/
        would be owned by microscope1_machine and unreadable by harry over VPN,
        even though it is harry's data.
        """
        user_dir = os.path.join(self.base_path, "users", user_id)
        self._run(["setfacl", "-d", "-m", f"u:{user_id}:rwx", user_dir])
        logger.info("provision: set default ACL on %s", user_dir)

    def _add_to_group(self, user_id: str, group_id: str) -> None:
        if not group_id:
            return
        self._run(["usermod", "-aG", group_id, user_id])
        logger.info("provision: added %s to group %s", user_id, group_id)

    def _set_quota(self, user_id: str) -> None:
        # setquota units are KiB; spec uses MiB so convert
        soft_kb = self.quota_soft_mb * 1024
        hard_kb = self.quota_hard_mb * 1024
        self._run([
            "setquota", "-u", user_id,
            str(soft_kb), str(hard_kb),
            "0", "0",           # inode soft/hard (0 = no limit)
            self.base_path,
        ])
        logger.info(
            "provision: quota set user=%s soft=%sMiB hard=%sMiB",
            user_id, self.quota_soft_mb, self.quota_hard_mb,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """
        Run a shell command. Never uses shell=True — all arguments are passed
        as a list to prevent injection via crafted user_id or group_id values.
        """
        if self.dry_run:
            logger.info("provision [DRY RUN]: %s", " ".join(cmd))
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

        logger.debug("provision: running %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            check=check,
            capture_output=True,
            text=True,
        )
