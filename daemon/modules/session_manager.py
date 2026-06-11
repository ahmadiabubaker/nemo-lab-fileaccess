import threading


class SessionManager:
    """
    Thread-safe in-memory session store.
    Tracks which machines each user is currently logged into.
    """

    def __init__(self):
        self._sessions: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def add(self, user_id: str, machine_id: str) -> None:
        with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = []
            if machine_id not in self._sessions[user_id]:
                self._sessions[user_id].append(machine_id)

    def remove(self, user_id: str, machine_id: str) -> None:
        with self._lock:
            if user_id in self._sessions:
                self._sessions[user_id] = [
                    m for m in self._sessions[user_id] if m != machine_id
                ]
                if not self._sessions[user_id]:
                    del self._sessions[user_id]

    def get_machines(self, user_id: str) -> list[str]:
        with self._lock:
            return list(self._sessions.get(user_id, []))

    def get_user(self, machine_id: str) -> str | None:
        with self._lock:
            for user_id, machines in self._sessions.items():
                if machine_id in machines:
                    return user_id
            return None

    def all_sessions(self) -> dict:
        with self._lock:
            return {uid: list(machines) for uid, machines in self._sessions.items()}
