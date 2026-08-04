"""What the MediaWiki-backed sources share.

Wikivoyage and Wikipedia are the same API behind different hostnames, so their
clients had the same request loop, the same retry set and the same sentence splitter
copied into both. The copies had already drifted — one ``_first_sentence`` stripped
its no-match return and the other did not, for no reason anyone chose.
"""

from __future__ import annotations

import json
import re
import time

import httpx

# Identifies this application per the Wikimedia user-agent policy. A library default
# or a browser-impersonating string is a policy violation.
USER_AGENT = "travel-planner/0.1 (+https://github.com/AnotherSava/travel-planner; POI discovery)"

RETRY_STATUSES = frozenset({429, 502, 503, 504})

_SENTENCE_END = re.compile(r"(?<![A-Z])(?<!\b[A-Z]\w)\.(?:\s|$)")


def first_sentence(text: str) -> str:
    """First sentence, without splitting on an abbreviation or an initial.

    The lookbehinds keep "St. Joseph Shrine" and "designed by Fr. J. Smith" whole,
    which a naive split on ``". "`` does not.
    """
    match = _SENTENCE_END.search(text)
    return text[: match.start() + 1].strip() if match else text.strip()


class MediaWikiError(RuntimeError):
    """A response that must not be cached or used."""


class MediaWikiClient:
    """A polite, retrying JSON client for one MediaWiki API endpoint.

    Subclasses supply the endpoint and their own error type; everything below is the
    behaviour both sources need — serialised access, backoff on the statuses
    Wikimedia sheds load with, and rejection of the two failure shapes that are not
    HTTP errors: a non-JSON body, and a 200 carrying an ``error`` object.
    """

    endpoint: str = ""
    error_type: type[MediaWikiError] = MediaWikiError

    def __init__(self, *, timeout: float = 60.0, max_retries: int = 3) -> None:
        self.max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, params: dict[str, str | int], *, url: str | None = None) -> dict:
        target = url or self.endpoint
        params = {"format": "json", "formatversion": "2", **params}
        last: Exception | None = None

        for attempt in range(self.max_retries):
            if attempt:
                time.sleep(2**attempt)
            try:
                reply = self._client.get(target, params=params)
            except httpx.HTTPError as exc:
                last = exc
                continue
            if reply.status_code in RETRY_STATUSES:
                last = self.error_type(f"HTTP {reply.status_code} from {target}")
                continue
            reply.raise_for_status()
            try:
                payload = reply.json()
            except json.JSONDecodeError as exc:
                raise self.error_type(f"non-JSON body from {target}") from exc
            if "error" in payload:
                detail = payload["error"].get("info", payload["error"])
                raise self.error_type(f"api error: {detail}")
            return payload

        raise self.error_type(f"{target} failed after {self.max_retries} attempts") from last
