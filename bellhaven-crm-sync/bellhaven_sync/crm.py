"""Thin client for the Bellhaven CRM sandbox API (/api/v1, bearer token)."""
import requests

from . import config

# Fields the API accepts on PATCH/POST /accounts (from its validation message).
ACCOUNT_MUTABLE = {
    "name", "parent_id", "status", "note", "care_type", "phone", "billing_street",
    "billing_city", "billing_state", "billing_zip", "chow_current_account", "duplicate_of_account",
}


class CRMError(RuntimeError):
    pass


class CRMClient:
    def __init__(self, api_base=None, token=None, session=None):
        self.api_base = (api_base or config.API_BASE).rstrip("/")
        token = token or config.API_TOKEN
        if not token:
            raise CRMError("Set BELLHAVEN_API_TOKEN (see README).")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def _request(self, method, path, **kwargs):
        resp = self.session.request(method, f"{self.api_base}{path}", timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise CRMError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else None

    def _list(self, path, page_size=200):
        items, page = [], 1
        while True:
            data = self._request("GET", path, params={"page": page, "page_size": page_size})
            items.extend(data["data"])
            if not data["data"] or len(items) >= data["total"]:
                return items
            page += 1

    def list_accounts(self):
        return self._list("/accounts")

    def list_contacts(self):
        return self._list("/contacts")

    def find_accounts(self, **filters):
        """Server-side filtered search (q, city, state, zip, street, parent_id)."""
        return self._request("GET", "/accounts", params={**filters, "page_size": 200})["data"]

    def get_account(self, account_id):
        return self._request("GET", f"/accounts/{account_id}")

    def update_account(self, account_id, fields):
        bad = set(fields) - ACCOUNT_MUTABLE
        if bad:
            raise CRMError(f"Not mutable via API: {sorted(bad)}")
        return self._request("PATCH", f"/accounts/{account_id}", json=fields)

    def create_account(self, fields):
        bad = set(fields) - ACCOUNT_MUTABLE
        if bad:
            raise CRMError(f"Not settable via API: {sorted(bad)}")
        return self._request("POST", "/accounts", json=fields)

    def get_contact(self, contact_id):
        return self._request("GET", f"/contacts/{contact_id}")

    def update_contact(self, contact_id, fields):
        return self._request("PATCH", f"/contacts/{contact_id}", json=fields)
