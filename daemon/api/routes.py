import re
import logging
from flask import Blueprint, request, jsonify, current_app
from .auth import require_api_key

logger = logging.getLogger(__name__)

routes = Blueprint("routes", __name__)

SAFE_ID_PATTERN = re.compile(r"^[0-9]+$")          # numeric NEMO ids (user/account/project)
SAFE_MACHINE_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")  # machine_id, from config allowlist


def sanitize_numeric_id(value, field_name: str) -> int:
    """Raises ValueError unless value is a positive integer (or numeric string)."""
    s = str(value)
    if not SAFE_ID_PATTERN.match(s):
        raise ValueError(f"Invalid characters in {field_name}: '{value}'")
    return int(s)


def sanitize_machine_id(value: str, allowed_machines: list[str]) -> str:
    """Raises ValueError if value is empty, contains anything outside
    [a-zA-Z0-9_], or is not present in the configured machine_id allowlist."""
    if not value or not SAFE_MACHINE_PATTERN.match(value):
        raise ValueError(f"Invalid characters in machine_id: '{value}'")
    if value not in allowed_machines:
        raise ValueError(f"Unknown machine_id: '{value}'")
    return value


@routes.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "message": "The Magic Butler is awake!"}), 200


@routes.route("/sessions", methods=["GET"])
@require_api_key
def get_sessions():
    session_mgr = current_app.session_manager
    state_db = current_app.state_db
    return jsonify({
        "active_sessions": session_mgr.all_sessions(),
        "db_sessions": state_db.get_active_sessions(),
    }), 200


@routes.route("/mount", methods=["POST"])
@require_api_key
def handle_mount():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    audit = current_app.audit_logger

    try:
        user_id = sanitize_numeric_id(data.get("user_id"), "user_id")
        machine_id = sanitize_machine_id(data.get("machine_id", ""), current_app.allowed_machines)
    except ValueError as e:
        logger.warning("mount rejected: %s", e)
        audit.log("tool_login", data.get("user_id"), data.get("machine_id"), "mount", "rejected", reason=str(e))
        return jsonify({"status": "error", "message": str(e)}), 400

    session_id = data.get("session_id", "")

    state_db = current_app.state_db
    session_mgr = current_app.session_manager
    mount_mgr = current_app.mount_manager

    projects = state_db.get_memberships(user_id)
    project_ids = [p["project_id"] for p in projects]
    username = state_db.get_username(user_id)

    state_db.open_session(user_id, machine_id, project_ids)
    session_mgr.add(user_id, machine_id)

    machine_account = f"{machine_id}_machine"
    ok = mount_mgr.mount(user_id=user_id, username=username, machine_id=machine_id,
                          projects=projects, machine_account=machine_account)
    if not ok:
        logger.error("mount: filesystem ops failed user=%s machine=%s", user_id, machine_id)

    audit.log("tool_login", user_id, machine_id, "mount", "success" if ok else "error",
              session_id=session_id, project_ids=project_ids)

    logger.info("mount: user=%s machine=%s session=%s", user_id, machine_id, session_id)
    return jsonify({
        "status": "success",
        "message": f"{user_id} logged into {machine_id}",
        "active_sessions": session_mgr.all_sessions(),
    }), 200


@routes.route("/unmount", methods=["POST"])
@require_api_key
def handle_unmount():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    audit = current_app.audit_logger

    try:
        user_id = sanitize_numeric_id(data.get("user_id"), "user_id")
        machine_id = sanitize_machine_id(data.get("machine_id", ""), current_app.allowed_machines)
    except ValueError as e:
        logger.warning("unmount rejected: %s", e)
        audit.log("tool_logout", data.get("user_id"), data.get("machine_id"), "unmount", "rejected", reason=str(e))
        return jsonify({"status": "error", "message": str(e)}), 400

    session_id = data.get("session_id", "")

    state_db = current_app.state_db
    session_mgr = current_app.session_manager
    idle_monitor = current_app.idle_monitor

    # Only touch the DB if the session actually exists
    existing = state_db.get_session(user_id, machine_id)
    if existing and existing["status"] == "active":
        state_db.begin_unmount(user_id, machine_id)
        project_ids = existing.get("project_ids", [])
    else:
        if not existing:
            logger.warning("unmount: no DB session found for user=%s machine=%s", user_id, machine_id)
        project_ids = []

    all_projects = state_db.get_projects()
    projects = [p for p in all_projects if p["project_id"] in project_ids]
    username = state_db.get_username(user_id)

    # The actual unmount (and session_manager.remove/close_session) runs in a
    # background thread via IdleMonitor, which waits for active write handles
    # to clear before unmounting and re-checks for a ghost (re-login) session.
    idle_monitor.start_unmount(user_id, machine_id, projects, username=username)

    audit.log("tool_logout", user_id, machine_id, "unmount", "queued",
              session_id=session_id, project_ids=project_ids)

    logger.info("unmount: user=%s machine=%s session=%s (unmount queued)", user_id, machine_id, session_id)
    return jsonify({
        "status": "success",
        "message": f"{user_id} logout from {machine_id} queued",
        "active_sessions": session_mgr.all_sessions(),
    }), 200


@routes.route("/provision", methods=["POST"])
@require_api_key
def handle_provision():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    audit = current_app.audit_logger

    try:
        user_id = sanitize_numeric_id(data.get("user_id"), "user_id")
    except ValueError as e:
        logger.warning("provision rejected: %s", e)
        audit.log("provision", data.get("user_id"), None, "provision", "rejected", reason=str(e))
        return jsonify({"status": "error", "message": str(e)}), 400

    full_name = data.get("full_name", "")
    username = data.get("username", f"u{user_id}")

    provisioner = current_app.user_provisioner
    ok = provisioner.provision(user_id=user_id, full_name=full_name)

    if ok:
        current_app.state_db.upsert_user(user_id, username, full_name)

    audit.log("provision", user_id, None, "provision", "success" if ok else "error", full_name=full_name)

    if not ok:
        return jsonify({"status": "error", "message": f"Provisioning failed for {user_id}"}), 500

    nemo_sync = getattr(current_app, "nemo_sync", None)
    if nemo_sync is not None:
        try:
            nemo_sync.sync_user_now(user_id)
            audit.log("provision", user_id, None, "sync_memberships", "success")
        except Exception as e:
            logger.error("provision: sync_user_now failed for user_id=%s: %s", user_id, e)
            audit.log("provision", user_id, None, "sync_memberships", "error", reason=str(e))

    return jsonify({
        "status": "success",
        "message": f"User {user_id} provisioned",
        "user_id": user_id,
    }), 200
