"""A small synchronous client for the RAGCentre HTTP API, used by the CLI (section 17).

It speaks only to the API: it never touches the data directory. ``RAGCENTRE_URL`` (default
``http://127.0.0.1:8700``) and ``RAGCENTRE_API_TOKEN`` configure it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8700"


class ApiError(Exception):
    """The API answered with an error body ``{"error": {"code", "message", "details"}}``."""

    def __init__(self, status: int, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})


class Unreachable(Exception):
    """No RAGCentre server answered at the configured URL."""


class Client:
    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 120.0,
        http: httpx.Client | None = None,
    ) -> None:
        self.url = (url or os.environ.get("RAGCENTRE_URL") or DEFAULT_URL).rstrip("/")
        token = token if token is not None else os.environ.get("RAGCENTRE_API_TOKEN")
        headers = {"User-Agent": "ragcentre-cli"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = headers

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- plumbing -----------------------------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.url}/v1{path}"
        try:
            resp = self._http.request(method, url, headers=self._headers, **kwargs)
        except httpx.TransportError as exc:
            raise Unreachable(
                f"Could not reach RAGCentre at {self.url} ({exc}). Is `ragcentre serve` running? "
                "Set RAGCENTRE_URL if it listens elsewhere."
            ) from None
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error") or {}
            except (ValueError, AttributeError):
                err = {}
            raise ApiError(
                resp.status_code,
                str(err.get("code") or f"http_{resp.status_code}"),
                str(err.get("message") or resp.reason_phrase),
                err.get("details") if isinstance(err.get("details"), dict) else None,
            )
        return resp

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self.request(method, path, **kwargs)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return self.call("POST", path, json=json, **kwargs)

    def put(self, path: str, json: Any = None) -> Any:
        return self.call("PUT", path, json=json)

    def delete(self, path: str) -> Any:
        return self.call("DELETE", path)

    def paginate(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            page = self.get(path, limit=500, cursor=cursor, **params)
            yield from page["items"]
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # ---- name resolution ----------------------------------------------------------------------

    def provider(self, ref: str) -> dict[str, Any]:
        return _pick("provider", ref, list(self.paginate("/providers")))

    def corpus(self, ref: str) -> dict[str, Any]:
        return _pick("corpus", ref, list(self.paginate("/corpora")))

    def pipeline(self, ref: str, corpus_id: str | None = None) -> dict[str, Any]:
        """A pipeline by id, or by name (within ``corpus_id`` when given, else across corpora)."""
        try:
            found: dict[str, Any] = self.get(f"/pipelines/{ref}")
            if corpus_id is None or found["corpus_id"] == corpus_id:
                return found
        except ApiError as exc:
            if exc.status != 404:
                raise
        corpus_ids = [corpus_id] if corpus_id else [c["id"] for c in self.paginate("/corpora")]
        candidates: list[dict[str, Any]] = []
        for cid in corpus_ids:
            for archived in (False, True):
                candidates += self.paginate(f"/corpora/{cid}/pipelines", archived=str(archived).lower())
        return _pick("pipeline", ref, candidates)

    def dataset(self, ref: str) -> dict[str, Any]:
        try:
            found: dict[str, Any] = self.get(f"/eval/datasets/{ref}")
            return found
        except ApiError as exc:
            if exc.status != 404:
                raise
        candidates: list[dict[str, Any]] = []
        for c in self.paginate("/corpora"):
            candidates += self.paginate(f"/corpora/{c['id']}/eval/datasets")
        return _pick("dataset", ref, candidates)


def _pick(kind: str, ref: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = [i for i in items if i.get("id") == ref]
    if by_id:
        return by_id[0]
    by_name = [i for i in items if i.get("name") == ref]
    if len(by_name) == 1:
        return by_name[0]
    if not by_name:
        raise ApiError(404, "not_found", f"No {kind} named or with id {ref!r}.")
    ids = ", ".join(i["id"] for i in by_name)
    raise ApiError(409, "conflict", f"{len(by_name)} {kind}s are named {ref!r} ({ids}); use an id instead.")
