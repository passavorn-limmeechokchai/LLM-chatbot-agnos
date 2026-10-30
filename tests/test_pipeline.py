import copy
import json

import httpx
import pytest

from agnos_rag.schemas import parse_entry
from agnos_rag.scraper import SourceError, scrape
from agnos_rag.storage import Store
from conftest import entry, html_data, listing_response


def test_rerun_is_idempotent_and_edit_updates_one_row(tmp_path, client_factory, listing_server):
    records = [entry(i) for i in range(1, 13)]
    handler, requests = listing_server(records)
    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        first = scrape(client, store, limit=10)
        before = dict(store.connection.execute("SELECT * FROM posts WHERE id=1").fetchone())
        second = scrape(client, store, limit=10)
        same = dict(store.connection.execute("SELECT * FROM posts WHERE id=1").fetchone())
        records[0]["doctor_comments"][0]["content_text"] = "คำตอบทดสอบที่แก้ไขแล้ว"
        third = scrape(client, store, limit=10)
        after = dict(store.connection.execute("SELECT * FROM posts WHERE id=1").fetchone())
        assert first["new_count"] == 10 and first["pages_fetched"] == 2
        assert second["unchanged_count"] == 10 and second["new_count"] == 0
        assert third["updated_count"] == 1 and third["unchanged_count"] == 9
        assert store.stats()["posts"] == 10
        assert before["updated_at"] == same["updated_at"] != after["updated_at"]
        assert before["first_seen_at"] == after["first_seen_at"]
        assert before["content_hash"] != after["content_hash"]
        assert same["last_seen_run_id"] == second["id"]
        assert "PRIVATE_TEST_NAME" not in json.dumps(after, ensure_ascii=False)
    assert len(requests) == 7  # One discovery, two listing pages per run.


def test_hash_ignores_engagement_order_and_timestamp_but_tracks_content():
    raw = entry()
    raw["forum"]["custom_tags"] = [{"name": "แท็กเพิ่ม", "type": "disease"}]
    original = parse_entry(raw)
    changed = copy.deepcopy(raw)
    changed["forum"]["sum_likes"] = 500
    changed["forum"]["content_text"] = "\u200b  " + raw["forum"]["content_text"] + "\r\n"
    changed["doctor_comments"][0]["edit_date"] = "2026-01-01T00:00:00Z"
    changed["doctor_comments"][0]["user"]["firstname"] = "another author"
    changed["comments"] = []
    changed["forum"]["default_tags"], changed["forum"]["custom_tags"] = (
        changed["forum"]["custom_tags"],
        changed["forum"]["default_tags"],
    )
    assert original.content_hash == parse_entry(changed).content_hash
    changed["doctor_comments"][0]["content_text"] += " เพิ่มเติม"
    assert original.content_hash != parse_entry(changed).content_hash


def test_pending_becomes_answered(tmp_path):
    with Store(tmp_path / "forum.sqlite3") as store:
        run_id = store.start_run(1)
        assert store.upsert(parse_entry(entry(answered=False)), run_id) == "new"
        assert store.stats()["pending"] == 1
        assert store.upsert(parse_entry(entry(answered=True)), run_id) == "updated"
        assert store.stats()["answered"] == 1
        assert store.stats()["pending"] == 0


def test_full_listing_stops_at_total_without_extra_page(tmp_path, client_factory, listing_server):
    handler, requests = listing_server([entry(i) for i in range(1, 18)])
    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        result = scrape(client, store, limit=None)
        assert result["status"] == "completed"
        assert result["new_count"] == 17
        assert result["pages_fetched"] == 3
    assert len(requests) == 4


def test_invalid_record_is_logged_without_patient_text(tmp_path, client_factory, listing_server):
    invalid = entry(2)
    invalid["forum"]["user_age"] = "PATIENT_SECRET"
    handler, _ = listing_server([entry(), invalid])
    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        result = scrape(client, store)
        assert result["status"] == "partial"
        assert result["new_count"] == result["failed_count"] == 1
        errors = store.stats()["latest_errors"]
        assert errors[0]["forum_id"] == 2
        assert "user_age" in errors[0]["detail"]
        assert "PATIENT_SECRET" not in json.dumps(errors)


def test_incomplete_answer_does_not_overwrite_stored_answer(tmp_path, client_factory, listing_server):
    records = [entry()]
    handler, _ = listing_server(records)
    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        scrape(client, store)
        before = dict(store.connection.execute("SELECT * FROM posts").fetchone())
        records[0]["doctor_comments"] = []
        result = scrape(client, store)
        after = dict(store.connection.execute("SELECT * FROM posts").fetchone())
        assert result["failed_count"] == 1
        assert before == after


def test_build_id_is_rediscovered_after_404(client_factory):
    discoveries = 0
    paths = []

    def handler(request):
        nonlocal discoveries
        paths.append(request.url.path)
        if request.url.path == "/forums":
            discoveries += 1
            return httpx.Response(200, text=html_data({"buildId": "old" if discoveries == 1 else "new"}))
        if "/old/" in request.url.path:
            return httpx.Response(404)
        return listing_response([entry()])

    client, _ = client_factory(handler)
    assert client.listing(1).total == 1
    assert client.build_id == "new"
    assert len(paths) == 4


@pytest.mark.parametrize("broken_json", [False, True])
def test_listing_falls_back_to_embedded_html(client_factory, broken_json):
    def handler(request):
        if request.url.path == "/forums":
            return httpx.Response(200, text=html_data({"buildId": "test"}))
        if request.url.path.endswith(".json"):
            return httpx.Response(200, text="invalid JSON") if broken_json else httpx.Response(404)
        return httpx.Response(
            200,
            text=html_data(
                {
                    "props": {
                        "pageProps": {
                            "forums": [entry()],
                            "total": 1,
                            "page": 1,
                        }
                    }
                }
            ),
        )

    client, _ = client_factory(handler)
    assert client.listing(1).forums[0]["forum"]["id"] == 1


@pytest.mark.parametrize("failure", [429, 503, "timeout"])
def test_retry_and_request_spacing(client_factory, failure):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(failure, headers={"Retry-After": "3"})
        if request.url.path == "/forums":
            return httpx.Response(200, text=html_data({"buildId": "test"}))
        return listing_response([entry()])

    client, clock = client_factory(handler)
    assert client.listing(1).total == 1
    assert attempts == 3
    assert clock.sleeps == ([1, 1] if failure == "timeout" else [3, 1])


def test_failure_preserves_previous_page_and_finishes_run(tmp_path, client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/forums":
            return httpx.Response(200, text=html_data({"buildId": "test"}))
        if request.url.params["page"] == "2":
            return httpx.Response(403, text="PRIVATE_RESPONSE")
        return listing_response([entry()], total=2)

    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        result = scrape(client, store, limit=None)
        assert result["status"] == "failed" and result["finished_at"]
        assert result["new_count"] == 1 and store.stats()["posts"] == 1
        assert "HTTP 403" in store.stats()["latest_errors"][0]["detail"]
        assert "PRIVATE_RESPONSE" not in json.dumps(store.stats())
    assert len(calls) == 3  # A forbidden response is not retried.


def test_duplicate_pages_fail_instead_of_looping(tmp_path, client_factory):
    def handler(request):
        if request.url.path == "/forums":
            return httpx.Response(200, text=html_data({"buildId": "test"}))
        return listing_response([entry()], page=int(request.url.params["page"]), total=10)

    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        result = scrape(client, store, limit=None)
        assert result["status"] == "failed"
        assert result["pages_fetched"] == 2 and result["new_count"] == 1


def test_wrong_page_is_rejected(client_factory):
    def handler(request):
        if request.url.path == "/forums":
            return httpx.Response(200, text=html_data({"buildId": "test"}))
        return listing_response([entry()], page=1)

    client, _ = client_factory(handler)
    with pytest.raises(SourceError, match="Requested page 2"):
        client.listing(2)


def test_retries_are_bounded(tmp_path, client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        result = scrape(client, store)
        assert result["status"] == "failed" and result["failed_count"] == 1
        assert result["pages_fetched"] == 0
    assert len(calls) == 3


def test_long_retry_after_stops_without_retrying_early(tmp_path, client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    client, clock = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        result = scrape(client, store)
        assert result["status"] == "failed"
        assert "run again later" in store.stats()["latest_errors"][0]["detail"]
    assert len(calls) == 1 and clock.sleeps == []


def test_keyboard_interrupt_is_logged_and_preserves_rows(tmp_path, client_factory):
    def handler(request):
        if request.url.path == "/forums":
            return httpx.Response(200, text=html_data({"buildId": "test"}))
        if request.url.params["page"] == "2":
            raise KeyboardInterrupt
        return listing_response([entry()], total=2)

    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        with pytest.raises(KeyboardInterrupt):
            scrape(client, store, limit=None)
        assert store.stats()["latest_run"]["status"] == "interrupted"
        assert store.stats()["posts"] == 1


def test_max_pages_caps_requests(tmp_path, client_factory, listing_server):
    handler, requests = listing_server([entry(i) for i in range(1, 21)])
    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        result = scrape(client, store, limit=None, max_pages=1)
        assert result["status"] == "completed"
        assert result["new_count"] == 8
    assert len(requests) == 2


def test_empty_listing_is_valid(tmp_path, client_factory, listing_server):
    handler, requests = listing_server([])
    client, _ = client_factory(handler)
    with Store(tmp_path / "forum.sqlite3") as store:
        result = scrape(client, store, limit=None)
        assert result["status"] == "completed" and result["new_count"] == 0
        assert store.stats()["posts"] == 0
    assert len(requests) == 2
