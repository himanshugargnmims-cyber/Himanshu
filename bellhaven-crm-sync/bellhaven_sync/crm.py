"""Thin client for the Bellhaven CRM sandbox API."""
import requests

from . import config


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

    def list_accounts(self, page_size=100):
        """Every account in the CRM, following pagination."""
        accounts, page = [], 1
        while True:
            data = self._request("GET", "/accounts", params={"page": page, "page_size": page_size})
            batch = _items(data)
            accounts.extend(batch)
            if not batch or len(batch) < page_size or not _has_more(data, page, len(accounts)):
                return accounts
            page += 1

    def get_account(self, account_id):
        return _unwrap(self._request("GET", f"/accounts/{account_id}"))

    def update_account(self, account_id, fields):
        return _unwrap(self._request("PATCH", f"/accounts/{account_id}", json=fields))

    def create_account(self, fields):
        return _unwrap(self._request("POST", "/accounts", json=fields))


def _items(data):
    if isinstance(data, list):
        return data
    for key in ("data", "items", "accounts", "results"):
        if isinstance(data.get(key), list):
            return data[key]
    raise CRMError(f"Unrecognised list response shape: {list(data)[:10]}")


def _has_more(data, page, fetched):
    if isinstance(data, list):
        return True
    if "has_more" in data:
        return bool(data["has_more"])
    total = data.get("total") or data.get("count")
    if total is not None:
        return fetched < int(total)
    pages = data.get("pages") or data.get("total_pages")
    return pages is None or page < int(pages)


def _unwrap(data):
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data
