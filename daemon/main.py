import logging
import os
import threading
import time
from flask import Flask
from daemon.api.routes import routes
from daemon.modules.config_loader import load_config, machine_ids
from daemon.modules.session_manager import SessionManager
from daemon.modules.state_db import StateDB
from daemon.modules.user_provisioner import UserProvisioner
from daemon.modules.mount_manager import MountManager
from daemon.modules.samba_controller import SambaController
from daemon.modules.idle_monitor import IdleMonitor
from daemon.modules.nemo_api_client import NemoApiClient
from daemon.modules.nemo_sync import NemoSync
from daemon.modules.audit_logger import AuditLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DRY_RUN = os.environ.get("LABFILES_DRY_RUN", "0") == "1"


def create_app() -> Flask:
    config = load_config()
    app = Flask(__name__)
    app.config_data = config

    app.allowed_machines = machine_ids(config)
    app.audit_logger = AuditLogger(
        log_path=config["logging"]["log_path"],
        rotation_days=config["logging"]["rotation_days"],
    )
    app.session_manager = SessionManager()
    app.state_db = StateDB(db_path=config["sessions"]["db_path"])
    app.user_provisioner = UserProvisioner(
        base_path=config["storage"]["base_path"],
        quota_soft_mb=config["storage"]["quota_soft_mb"],
        quota_hard_mb=config["storage"]["quota_hard_mb"],
        dry_run=DRY_RUN,
    )
    app.mount_manager = MountManager(
        base_path=config["storage"]["base_path"],
        sessions_path=config["sessions"]["mount_base_path"],
        dry_run=DRY_RUN,
    )
    app.samba_controller = SambaController(status_command=config["samba"]["status_command"], dry_run=DRY_RUN)
    app.idle_monitor = IdleMonitor(
        samba_controller=app.samba_controller,
        mount_manager=app.mount_manager,
        session_manager=app.session_manager,
        state_db=app.state_db,
        check_interval_seconds=config["idle_monitor"]["check_interval_seconds"],
        max_idle_timeout_seconds=config["idle_monitor"]["max_idle_timeout_seconds"],
        audit_logger=app.audit_logger,
    )

    _recover_orphaned_sessions(app.state_db, app.mount_manager, app.session_manager)

    nemo_config = config["nemo_sync"]
    if nemo_config["api_base_url"] and nemo_config["api_token"]:
        nemo_api_client = NemoApiClient(base_url=nemo_config["api_base_url"], api_token=nemo_config["api_token"])
        app.nemo_sync = NemoSync(
            nemo_api_client=nemo_api_client,
            state_db=app.state_db,
            user_provisioner=app.user_provisioner,
            base_path=config["storage"]["base_path"],
            on_deactivation=nemo_config["on_deactivation"],
            dry_run=DRY_RUN,
            audit_logger=app.audit_logger,
        )
        _start_nemo_sync_loop(app.nemo_sync, nemo_config["poll_interval_seconds"])
    else:
        logger.warning("NemoSync disabled: nemo_sync.api_base_url/api_token not set in config")

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


def _start_nemo_sync_loop(nemo_sync: NemoSync, poll_interval_seconds: int) -> None:
    """Runs NemoSync.run_once() once at startup, then on a fixed interval
    in a background thread. Does not block app startup."""

    def loop():
        while True:
            try:
                nemo_sync.run_once()
            except Exception:
                logger.exception("NemoSync: run_once failed")
            time.sleep(poll_interval_seconds)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


app = create_app()

if __name__ == "__main__":
    host = app.config_data["server"]["host"]
    port = app.config_data["server"]["port"]
    app.run(host=host, port=port)
