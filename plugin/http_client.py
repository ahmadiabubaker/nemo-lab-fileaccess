import logging
import requests

logger = logging.getLogger(__name__)


class DaemonClient:
    """
    HTTPS (mutual TLS) client for the lab file access daemon's API.

    base_url must be an https:// URL. client_cert is (cert_path, key_path),
    used for mTLS; ca_cert verifies the daemon's server certificate.
    """

    def __init__(self, base_url: str, api_key: str,
                 client_cert: tuple[str, str] | None = None,
                 ca_cert: str | None = None, timeout_seconds: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key
        if client_cert and client_cert[0] and client_cert[1]:
            self.session.cert = client_cert
        if ca_cert:
            self.session.verify = ca_cert

    def _post(self, endpoint: str, payload: dict) -> bool:
        try:
            resp = self.session.post(f"{self.base_url}/{endpoint}", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            logger.info("labfiles: POST /%s -> %s %s", endpoint, resp.status_code, payload)
            return True
        except Exception as e:
            logger.error("labfiles: POST /%s failed: %s (payload=%s)", endpoint, e, payload)
            return False

    def provision(self, user_id: int, username: str, full_name: str, email: str = "") -> bool:
        return self._post("provision", {
            "event": "user_created",
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "email": email,
        })

    def mount(self, user_id: int, machine_id: str, session_id: str) -> bool:
        return self._post("mount", {
            "event": "tool_login",
            "user_id": user_id,
            "machine_id": machine_id,
            "session_id": session_id,
        })

    def unmount(self, user_id: int, machine_id: str, session_id: str) -> bool:
        return self._post("unmount", {
            "event": "tool_logout",
            "user_id": user_id,
            "machine_id": machine_id,
            "session_id": session_id,
        })
