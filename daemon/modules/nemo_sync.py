import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class NemoSync:
    """
    Periodically polls the NEMO API to keep local account/project/membership
    state in sync, independent of the user_created/tool_login/tool_logout
    event stream.

    Runs on a fixed interval (nemo_sync.poll_interval_seconds in config).
    Active sessions are not disrupted by a run: membership changes (including
    renames) affect the next tool_login, not currently mounted sessions —
    physical paths are ID-based and never move.
    """

    def __init__(self, nemo_api_client, state_db, user_provisioner,
                 base_path: str = "/srv/labdata", on_deactivation: str = "lock_account",
                 dry_run: bool = False):
        self.nemo_api_client = nemo_api_client
        self.state_db = state_db
        self.user_provisioner = user_provisioner
        self.base_path = base_path
        self.on_deactivation = on_deactivation
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> None:
        accounts = {a["id"]: a for a in self.nemo_api_client.get_accounts()}
        projects = {p["id"]: p for p in self.nemo_api_client.get_projects()}
        users = self.nemo_api_client.get_users()

        self._sync_accounts_and_projects(accounts, projects)
        self._sync_users(users)
        self._sync_memberships(users)

        logger.info("NemoSync: run_once complete (accounts=%d projects=%d users=%d)",
                     len(accounts), len(projects), len(users))

    # ------------------------------------------------------------------
    # Accounts & projects
    # ------------------------------------------------------------------

    def _sync_accounts_and_projects(self, accounts: dict, projects: dict) -> None:
        for account_id, account in accounts.items():
            self.state_db.upsert_account(account_id, account["name"])

        for project_id, project in projects.items():
            account_id = project["account"]
            linux_group = f"proj_{project_id}"
            path = self._project_path(account_id, project_id)

            self._ensure_account_dir(account_id)
            self._ensure_project_dir(account_id, project_id, linux_group)

            self.state_db.upsert_project(
                project_id=project_id,
                account_id=account_id,
                name=project["name"],
                linux_group=linux_group,
                path=path,
            )

            if not project.get("active", True) or not accounts.get(account_id, {}).get("active", True):
                self._handle_deactivated_project(project_id, linux_group)

    def _ensure_account_dir(self, account_id: int) -> None:
        path = os.path.join(self.base_path, "groups", f"account_{account_id}")
        self._run(["mkdir", "-p", path])
        self._run(["chown", "root:root", path])
        self._run(["chmod", "0711", path])

    def _ensure_project_dir(self, account_id: int, project_id: int, linux_group: str) -> None:
        path = self._project_path(account_id, project_id)

        result = self._run(["getent", "group", linux_group], check=False)
        if result.returncode != 0:
            self._run(["groupadd", linux_group])
            logger.info("NemoSync: created group %s", linux_group)

        self._run(["mkdir", "-p", path])
        self._run(["chown", f"root:{linux_group}", path])
        self._run(["chmod", "2770", path])
        self._run(["setfacl", "-d", "-m", f"g:{linux_group}:rwx", path])

    def _project_path(self, account_id: int, project_id: int) -> str:
        return os.path.join(self.base_path, "groups", f"account_{account_id}", f"project_{project_id}")

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def _sync_users(self, users: list[dict]) -> None:
        for user in users:
            user_id = user["id"]
            username = f"u{user_id}"

            result = self._run(["id", username], check=False)
            if result.returncode != 0:
                full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                logger.info("NemoSync: provisioning new user %s (id=%s)", username, user_id)
                self.user_provisioner.provision(user_id=user_id, full_name=full_name)

            if not user.get("is_active", True):
                self._handle_deactivated_user(user_id, username)

    def _handle_deactivated_user(self, user_id: int, username: str) -> None:
        if self.on_deactivation == "lock_account":
            self._run(["usermod", "-L", username], check=False)
            logger.info("NemoSync: locked deactivated user %s", username)
        elif self.on_deactivation == "remove_membership_only":
            self.state_db.set_memberships(user_id, [])
            logger.info("NemoSync: cleared memberships for deactivated user %s", username)
        elif self.on_deactivation == "ignore":
            pass
        else:
            logger.warning("NemoSync: unknown on_deactivation policy '%s'", self.on_deactivation)

    def _handle_deactivated_project(self, project_id: int, linux_group: str) -> None:
        if self.on_deactivation == "lock_account":
            return
        elif self.on_deactivation == "remove_membership_only":
            for member in self.state_db.get_memberships_for_project(project_id):
                self._remove_membership(member["user_id"], project_id, linux_group)
        elif self.on_deactivation == "ignore":
            pass

    # ------------------------------------------------------------------
    # Memberships
    # ------------------------------------------------------------------

    def _sync_memberships(self, users: list[dict]) -> None:
        for user in users:
            user_id = user["id"]
            username = f"u{user_id}"
            new_project_ids = set(user.get("projects", []))

            old_project_ids = {m["project_id"] for m in self.state_db.get_memberships(user_id)}

            for project_id in new_project_ids - old_project_ids:
                self._add_membership(user_id, username, project_id)

            for project_id in old_project_ids - new_project_ids:
                linux_group = f"proj_{project_id}"
                self._remove_membership(user_id, project_id, linux_group, username=username)

            self.state_db.set_memberships(user_id, sorted(new_project_ids))

    def _add_membership(self, user_id: int, username: str, project_id: int) -> None:
        linux_group = f"proj_{project_id}"
        self._run(["usermod", "-aG", linux_group, username])
        logger.info("NemoSync: added %s to group %s (project %s)", username, linux_group, project_id)

    def _remove_membership(self, user_id: int, project_id: int, linux_group: str, username: str | None = None) -> None:
        username = username or f"u{user_id}"
        self._run(["gpasswd", "-d", username, linux_group], check=False)
        logger.info("NemoSync: removed %s from group %s (project %s)", username, linux_group, project_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        if self.dry_run:
            logger.info("NemoSync [DRY RUN]: %s", " ".join(cmd))
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

        logger.debug("NemoSync: running %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr}"
            )
        return result
