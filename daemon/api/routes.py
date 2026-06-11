import re
import logging
from flask import Blueprint, request, jsonify, current_app
from .auth import require_api_key

logger = logging.getLogger(__name__)

routes = Blueprint("routes", __name__)

SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
SLUGIFY_PATTERN = re.compile(r"[^a-zA-Z0-9]+")


def sanitize_id(value: str, field_name: str) -> str:
    """Raises ValueError for empty or path-traversal values.
    Slugifies spaces/hyphens into underscores so tool names like
    'JEOL E-beam' become 'JEOL_E_beam' rather than being rejected.
    Prevents path traversal: '../../../etc' → ValueError.
    """
    if not value:
        raise ValueError(f"{field_name} is empty")
    # Reject anything that looks like path traversal before slugifying
    if ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"Invalid characters in {field_name}: '{value}'")
    slugified = SLUGIFY_PATTERN.sub("_", value).strip("_")
    if not slugified:
        raise ValueError(f"Invalid {field_name}: '{value}'")
    return slugified


def _parse_and_sanitize(data: dict, *fields: str) -> dict:
    result = {}
    for field in fields:
        raw = data.get(field, "")
        result[field] = sanitize_id(str(raw), field)
    return result


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

    try:
        ids = _parse_and_sanitize(data, "user_id", "machine_id")
    except ValueError as e:
        logger.warning("mount rejected: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 400

    user_id = ids["user_id"]
    machine_id = ids["machine_id"]
    group_id = data.get("group_id", "")
    session_id = data.get("session_id", "")

    state_db = current_app.state_db
    session_mgr = current_app.session_manager
    mount_mgr = current_app.mount_manager

    state_db.open_session(user_id, machine_id, group_id)
    session_mgr.add(user_id, machine_id)

    machine_account = f"{machine_id}_machine"
    ok = mount_mgr.mount(user_id=user_id, machine_id=machine_id,
                         group_id=group_id, machine_account=machine_account)
    if not ok:
        logger.error("mount: filesystem ops failed user=%s machine=%s", user_id, machine_id)

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

    try:
        ids = _parse_and_sanitize(data, "user_id", "machine_id")
    except ValueError as e:
        logger.warning("unmount rejected: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 400

    user_id = ids["user_id"]
    machine_id = ids["machine_id"]
    session_id = data.get("session_id", "")

    state_db = current_app.state_db
    session_mgr = current_app.session_manager
    mount_mgr = current_app.mount_manager

    # Only touch the DB if the session actually exists
    existing = state_db.get_session(user_id, machine_id)
    if existing and existing["status"] == "active":
        state_db.begin_unmount(user_id, machine_id)
        group_id = existing.get("group_id", "")
    else:
        if not existing:
            logger.warning("unmount: no DB session found for user=%s machine=%s", user_id, machine_id)
        group_id = data.get("group_id", "")

    session_mgr.remove(user_id, machine_id)
    remaining = session_mgr.get_machines(user_id)

    machine_account = f"{machine_id}_machine"
    ok = mount_mgr.unmount(user_id=user_id, machine_id=machine_id,
                           group_id=group_id, machine_account=machine_account,
                           remaining_sessions=remaining)
    if not ok:
        logger.error("unmount: filesystem ops failed user=%s machine=%s", user_id, machine_id)

    if existing and existing["status"] == "active":
        state_db.close_session(user_id, machine_id)

    logger.info("unmount: user=%s machine=%s session=%s", user_id, machine_id, session_id)
    return jsonify({
        "status": "success",
        "message": f"{user_id} logged out of {machine_id}",
        "active_sessions": session_mgr.all_sessions(),
    }), 200


@routes.route("/provision", methods=["POST"])
@require_api_key
def handle_provision():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    try:
        ids = _parse_and_sanitize(data, "user_id")
    except ValueError as e:
        logger.warning("provision rejected: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 400

    user_id = ids["user_id"]
    group_id = data.get("group_id", "")
    full_name = data.get("full_name", "")

    provisioner = current_app.user_provisioner
    ok = provisioner.provision(user_id=user_id, group_id=group_id, full_name=full_name)

    if not ok:
        return jsonify({"status": "error", "message": f"Provisioning failed for {user_id}"}), 500

    return jsonify({
        "status": "success",
        "message": f"User {user_id} provisioned",
        "user_id": user_id,
        "group_id": group_id,
    }), 200
