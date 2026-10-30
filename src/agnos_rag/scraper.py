"""Polite, bounded Next.js listing scraper with JSON/HTML fallback."""

import json
import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

import httpx
from pydantic import ValidationError

from agnos_rag.schemas import BASE_URL, ListingPage, parse_entry
from agnos_rag.storage import Store


class SourceError(Exception):
    """A website response cannot safely be interpreted as forum data."""


class NextDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.chunks.append(data)


def next_data(html: str) -> dict:
    parser = NextDataParser()
    parser.feed(html)
    try:
        result = json.loads("".join(parser.chunks))
    except (ValueError, TypeError) as exc:
        raise SourceError("HTML has no valid __NEXT_DATA__ JSON") from exc
    if not isinstance(result, dict):
        raise SourceError("__NEXT_DATA__ must be an object")
    return result


class ForumClient:
    def __init__(
        self,
        *,
        interval: float = 1.0,
        attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if interval < 0 or attempts < 1:
            raise ValueError("interval must be nonnegative and attempts must be positive")
        self.interval = interval
        self.attempts = attempts
        self.sleep = sleep
        self.monotonic = monotonic
        self.last_request: float | None = None
        self.build_id: str | None = None
        self.http = httpx.Client(
            base_url=BASE_URL,
            timeout=30,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": "AgnosAssignmentScraper/0.1 (local research pipeline)"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def _get(self, path: str) -> httpx.Response:
        for attempt in range(self.attempts):
            if self.last_request is not None:
                delay = self.interval - (self.monotonic() - self.last_request)
                if delay > 0:
                    self.sleep(delay)
            self.last_request = self.monotonic()
            try:
                response = self.http.get(path)
            except httpx.TransportError:
                if attempt == self.attempts - 1:
                    raise
                self.sleep(min(2**attempt, 30))
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.attempts - 1:
                delay = float(2**attempt)
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            date = parsedate_to_datetime(retry_after)
                            delay = (date - datetime.now(UTC)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            pass
                if not math.isfinite(delay):
                    delay = float(2**attempt)
                if delay > 60:
                    raise SourceError("Server requests a wait longer than 60 seconds; run again later")
                self.sleep(max(0, delay))
                continue
            response.raise_for_status()
            return response
        raise AssertionError("unreachable retry state")

    def discover_build_id(self) -> str:
        data = next_data(self._get("/forums").text)
        build_id = data.get("buildId")
        if not isinstance(build_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", build_id):
            raise SourceError("Missing or invalid Next.js buildId")
        self.build_id = build_id
        return build_id

    def listing(self, page: int) -> ListingPage:
        if self.build_id is None:
            self.discover_build_id()
        data = None
        for attempt in range(2):
            try:
                response = self._get(f"/_next/data/{self.build_id}/th/forums/search.json?page={page}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                if attempt == 0:
                    self.discover_build_id()
                    continue
                break
            try:
                candidate = response.json()
            except ValueError:
                break
            if isinstance(candidate, dict) and isinstance(candidate.get("pageProps"), dict):
                data = candidate["pageProps"]
            break
        if data is None:
            # The page HTML can still work when a Next.js data route changes or disappears.
            embedded = next_data(self._get(f"/forums/search?page={page}").text)
            data = embedded.get("props", {}).get("pageProps")
        try:
            listing = ListingPage.model_validate(data)
        except ValidationError as exc:
            raise SourceError("Listing schema changed: expected forums, total and page") from exc
        if listing.page != page:
            raise SourceError(f"Requested page {page}, website returned page {listing.page}")
        return listing


def safe_error(exc: Exception) -> str:
    """Audit useful failure metadata without echoing patient input or response bodies."""
    if isinstance(exc, ValidationError):
        return json.dumps(
            [
                {"field": ".".join(map(str, error["loc"])), "type": error["type"]}
                for error in exc.errors(include_input=False, include_context=False)
            ]
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} at {exc.request.url.path}"
    if isinstance(exc, httpx.TransportError):
        return type(exc).__name__
    if isinstance(exc, SourceError):
        return str(exc)
    if isinstance(exc, ValueError) and str(exc) in {"empty_question", "answered_without_doctor_text"}:
        return str(exc)
    return type(exc).__name__


def scrape(
    client: ForumClient, store: Store, *, limit: int | None = 10, max_pages: int | None = None
) -> dict:
    if (limit is not None and limit < 1) or (max_pages is not None and max_pages < 1):
        raise ValueError("limit and max_pages must be positive")
    run_id = store.start_run(limit)
    counts = dict.fromkeys(("new", "updated", "unchanged", "failed"), 0)
    pages = 0
    seen: set[int] = set()
    attempted = 0
    page = 1
    status = "completed"
    try:
        while True:
            listing = client.listing(page)
            pages += 1
            if not listing.forums:
                if listing.total:
                    raise SourceError("Empty listing page despite a nonzero total")
                break
            before = len(seen)
            for raw in listing.forums:
                source_id = raw.get("forum", {}).get("id") if isinstance(raw.get("forum"), dict) else None
                forum_id = source_id if type(source_id) is int and source_id > 0 else None
                if forum_id is not None and forum_id in seen:
                    continue
                if forum_id is not None:
                    seen.add(forum_id)
                attempted += 1
                try:
                    post = parse_entry(raw)
                except (ValidationError, ValueError) as exc:
                    counts["failed"] += 1
                    store.record_error(run_id, page, type(exc).__name__, safe_error(exc), forum_id)
                else:
                    counts[store.upsert(post, run_id)] += 1
                if limit is not None and attempted >= limit:
                    break
            if limit is not None and attempted >= limit:
                break
            if len(seen) >= listing.total:
                break
            if max_pages is not None and pages >= max_pages:
                break
            if len(seen) == before:
                raise SourceError("Listing made no progress; refusing to crawl repeated pages")
            page += 1
    except (SourceError, httpx.HTTPError) as exc:
        counts["failed"] += 1
        store.record_error(run_id, page, type(exc).__name__, safe_error(exc))
        status = "failed"
    except BaseException as exc:
        counts["failed"] += 1
        store.record_error(run_id, page, type(exc).__name__, type(exc).__name__)
        store.finish_run(
            run_id, "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", pages, counts
        )
        raise
    if status == "completed" and counts["failed"]:
        status = "partial"
    return store.finish_run(run_id, status, pages, counts)
