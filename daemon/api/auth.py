import os
import functools
from flask import request, jsonify

API_KEY = os.environ.get("LABFILES_API_KEY", "dev_key")


def require_api_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key")
        if key != API_KEY:
            return jsonify({"status": "error", "message": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated
