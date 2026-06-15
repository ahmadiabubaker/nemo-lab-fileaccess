import requests


class NemoApiClient:
    """
    Thin client over the NEMO REST API.

    Returns plain dicts/lists as parsed from NEMO's JSON responses — see the
    sample exports in "Princeton Nemo Samples/" for the shape of each
    resource (accounts: id/name/active; projects: id/name/account/active;
    users: id/projects[]/is_active).
    """

    def __init__(self, base_url: str, api_token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {api_token}"
        self.timeout = timeout

    def get_accounts(self) -> list[dict]:
        return self._get_all("accounts")

    def get_projects(self) -> list[dict]:
        return self._get_all("projects")

    def get_users(self) -> list[dict]:
        return self._get_all("users")

    def get_user(self, user_id: int) -> dict:
        url = f"{self.base_url}/users/{user_id}/"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _get_all(self, resource: str) -> list[dict]:
        """
        Fetches all pages of a NEMO list endpoint. NEMO's DRF-style pagination
        returns {"next": <url or null>, "results": [...]}; some deployments
        return a bare list instead, so handle both.
        """
        results = []
        url = f"{self.base_url}/{resource}/"
        params = {}
        while url:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                results.extend(data)
                break
            results.extend(data.get("results", []))
            url = data.get("next")
            params = {}
        return results
