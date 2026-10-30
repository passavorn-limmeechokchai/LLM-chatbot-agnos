"""Entirely synthetic fixtures; no scraped patient data in the test suite."""

import json

import httpx
import pytest

from agnos_rag.scraper import ForumClient


def entry(post_id=1, *, answered=True):
    return {
        "forum": {
            "id": post_id,
            "pub_date": "2024-01-01T00:00:00Z",
            "user_age": 25,
            "user_gender": "female",
            "disease_key": "synthetic-disease",
            "disease_text": "หัวข้อทดสอบ",
            "content_text": f"คำถามทดสอบหมายเลข {post_id}",
            "default_tags": [{"name": "อาการทดสอบ", "type": "symptom", "symptom_key": "test"}],
            "custom_tags": [],
            "is_doctor_answered": answered,
            "sum_likes": 0,
        },
        "doctor_comments": [
            {
                "id": post_id * 10,
                "content_text": "คำตอบสังเคราะห์สำหรับทดสอบระบบ",
                "pub_date": "2024-01-01T00:00:00Z",
                "edit_date": None,
                "user": {"firstname": "ชื่อแพทย์ทดสอบ"},
            }
        ]
        if answered
        else [],
        "comments": [{"content_text": "ความเห็นทดสอบ", "user": {"firstname": "PRIVATE_TEST_NAME"}}],
    }


def html_data(data):
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def listing_response(records, *, page=1, total=None):
    return httpx.Response(
        200,
        json={
            "pageProps": {
                "forums": records,
                "page": page,
                "total": total if total is not None else len(records),
            }
        },
    )


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def monotonic(self):
        return self.now


@pytest.fixture
def client_factory():
    clients = []

    def make(handler):
        clock = Clock()
        client = ForumClient(
            transport=httpx.MockTransport(handler), sleep=clock.sleep, monotonic=clock.monotonic
        )
        clients.append(client)
        return client, clock

    yield make
    for client in clients:
        client.http.close()


@pytest.fixture
def listing_server():
    def make(records):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/forums":
                return httpx.Response(200, text=html_data({"buildId": "test-build"}))
            page = int(request.url.params["page"])
            return listing_response(records[(page - 1) * 8 : page * 8], page=page, total=len(records))

        return handler, requests

    return make
