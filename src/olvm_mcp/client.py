"""Minimal client for the OLVM / oVirt Engine REST API (v4, JSON).

Uses the engine's SSO token endpoint (the same flow as the official Python SDK)
and plain HTTPS, so it runs anywhere httpx does, including Windows.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300


class OlvmError(Exception):
    """An error talking to the engine, with a message safe to show to users."""


class OlvmAuthError(OlvmError):
    pass


class OlvmNotFound(OlvmError):
    pass


class OlvmClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._http = httpx.Client(
            base_url=settings.url,
            verify=settings.verify,
            timeout=settings.timeout,
            transport=transport,
            headers={"Accept": "application/json", "Version": "4"},
        )
        self._token: str | None = None
        self._name_cache: dict[str, tuple[float, dict[str, str]]] = {}

    # -- auth ---------------------------------------------------------------

    def _login(self) -> str:
        try:
            resp = self._http.post(
                "/sso/oauth/token",
                data={
                    "grant_type": "password",
                    "scope": "ovirt-app-api",
                    "username": self._settings.username,
                    "password": self._settings.password,
                },
            )
        except httpx.TransportError as e:
            raise self._transport_error(e) from e

        try:
            body = resp.json()
        except ValueError:
            body = {}
        token = body.get("access_token")
        if resp.status_code != 200 or not token:
            reason = body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}"
            raise OlvmAuthError(f"Login to the engine failed for {self._settings.username}: {reason}")
        self._token = token
        return token

    def close(self) -> None:
        """Revoke the SSO token (best effort) and close the connection."""
        if self._token:
            try:
                resp = self._http.post("/services/sso-logout",
                                       data={"scope": "ovirt-app-api", "token": self._token})
                if resp.status_code != 200:
                    log.debug("Token revoke returned HTTP %s", resp.status_code)
            except httpx.HTTPError:
                pass
            self._token = None
        self._http.close()

    # -- requests -----------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET /api/<path> and return the decoded JSON body."""
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any], correlation_id: str | None = None) -> dict[str, Any]:
        """POST a JSON body to /api/<path> (an action or a new object) and return the response.

        The correlation id is sent as the engine's Correlation-Id header, so the
        engine's events and jobs for this request carry it.
        """
        headers = {"Correlation-Id": correlation_id} if correlation_id else None
        return self._request("POST", path, json=body, headers=headers)

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 json: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"/api/{path.lstrip('/')}"
        for attempt in (1, 2):
            token = self._token or self._login()
            # A 401 means the engine rejected the token before running anything,
            # so retrying once is safe for POSTs too.
            try:
                resp = self._http.request(method, url, params=params, json=json,
                                          headers={**(headers or {}), "Authorization": f"Bearer {token}"})
            except httpx.TransportError as e:
                raise self._transport_error(e) from e
            if resp.status_code == 401 and attempt == 1:
                log.info("Token rejected; logging in again")
                self._token = None
                continue
            break
        return self._decode(resp)

    def _decode(self, resp: httpx.Response) -> dict[str, Any]:
        content_type = resp.headers.get("content-type", "")
        is_json = "json" in content_type

        if resp.status_code == 404:
            raise OlvmNotFound("Not found on the engine")
        if resp.status_code >= 400 or not is_json:
            detail = ""
            if is_json:
                body = resp.json()
                # Failed actions wrap the fault: {"status": "failed", "fault": {"detail": ...}}
                fault = body.get("fault") or body
                detail = fault.get("detail") or fault.get("reason") or ""
            elif "html" in content_type:
                detail = (
                    "the engine returned an HTML error page; this usually means the "
                    "user has no permissions in OLVM (assign a role such as ReadOnlyAdmin)"
                )
            raise OlvmError(f"Engine request failed (HTTP {resp.status_code}): {detail}".rstrip(": "))
        return resp.json() if resp.content else {}

    def _transport_error(self, e: httpx.TransportError) -> OlvmError:
        msg = f"Cannot reach the engine at {self._settings.url}: {e}"
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            msg += " (set OLVM_CA_FILE to the engine's CA certificate)"
        return OlvmError(msg)

    # -- collections --------------------------------------------------------

    def list(self, collection: str, key: str, search: str | None = None,
             max_results: int | None = None) -> list[dict[str, Any]]:
        """List a collection such as `vms` (JSON key `vm`), optionally filtered."""
        params: dict[str, Any] = {}
        if search:
            params["search"] = search
        if max_results is not None:
            params["max"] = max_results
        return self.get(collection, params or None).get(key, [])

    def names(self, collection: str, key: str) -> dict[str, str]:
        """Map of id -> name for a small collection (clusters, hosts), cached briefly."""
        now = time.monotonic()
        cached = self._name_cache.get(collection)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        mapping = {item["id"]: item.get("name", item["id"]) for item in self.list(collection, key)}
        self._name_cache[collection] = (now, mapping)
        return mapping
