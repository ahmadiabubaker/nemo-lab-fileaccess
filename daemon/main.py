import logging
import os
from flask import Flask
from daemon.api.routes import routes
from daemon.modules.session_manager import SessionManager
from daemon.modules.state_db import StateDB
from daemon.modules.user_provisioner import UserProvisioner
from daemon.modules.mount_manager import MountManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("LABFILES_DB_PATH", ":memory:")
BASE_PATH = os.environ.get("LABFILES_BASE_PATH", "/srv/labdata")
SESSIONS_PATH = os.environ.get("LABFILES_SESSIONS_PATH", "/mnt/labsessions")
DRY_RUN = os.environ.get("LABFILES_DRY_RUN", "0") == "1"

# Comma-separated allowlist of machine_ids, e.g. "microscope1,microscope2"
ALLOWED_MACHINES = [
    m.strip() for m in os.environ.get("LABFILES_MACHINES", "microscope1,microscope2").split(",")
    if m.strip()
]


def create_app() -> Flask:
    app = Flask(__name__)

    app.allowed_machines = ALLOWED_MACHINES
    app.session_manager = SessionManager()
    app.state_db = StateDB(db_path=DB_PATH)
    app.user_provisioner = UserProvisioner(base_path=BASE_PATH, dry_run=DRY_RUN)
    app.mount_manager = MountManager(base_path=BASE_PATH, sessions_path=SESSIONS_PATH, dry_run=DRY_RUN)

    _recover_orphaned_sessions(app.state_db, app.mount_manager, app.session_manager)

    app.register_blueprint(routes)
    return app


def _recover_orphaned_sessions(state_db: StateDB, mount_mgr: MountManager, session_mgr: SessionManager) -> None:
    """Run at startup before accepting requests."""
    orphans = state_db.get_active_sessions()
    if not orphans:
        return
    logger.warning("Recovering %d orphaned session(s) from previous run", len(orphans))

    all_projects = state_db.get_projects()
    for session in orphans:
        logger.warning("  orphan: user=%s machine=%s status=%s",
                        session["user_id"], session["machine_id"], session["status"])
        projects = [p for p in all_projects if p["project_id"] in session["project_ids"]]
        machine_account = f"{session['machine_id']}_machine"
        mount_mgr.unmount(
            session["user_id"], session["machine_id"],
            projects, machine_account, remaining_sessions=[],
        )
        session_mgr.remove(session["user_id"], session["machine_id"])
        state_db.close_session(session["user_id"], session["machine_id"])


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
