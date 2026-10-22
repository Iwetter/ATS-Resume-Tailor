from pathlib import Path
from dataclasses import replace
import json

import pytest

from hunter.http import SourceError, PublicClient
from hunter.models import Employer
from hunter.sources import fetch_board, normalize, employer_from_url
from hunter.storage import Store

FIXTURES = Path(__file__).parent / "fixtures/hunter"
NOW = "2026-10-20T16:00:00+00:00"


class FixtureClient:
    def __init__(self, source):
        self.source = source
        self.payload = json.loads((FIXTURES / (source+".json")).read_text())
        self.calls = []

    def json(self, url):
        self.calls.append(url)
        detail = self.payload.get("detail") if isinstance(self.payload, dict) else None
        is_detail = url.endswith("/101") or url.endswith("/fixture-sr-1")
        return (detail if is_detail else self.payload), NOW, False


@pytest.mark.parametrize("source", ["greenhouse", "lever", "ashby", "smartrecruiters"])
def test_source_adapters_with_saved_fixtures(source):
    client = FixtureClient(source)
    result = fetch_board(Employer("Fixture Engineering", source, "fixture", "https://example.com"), client)
    assert result.complete and len(result.jobs) == 1
    job = result.jobs[0]
    assert "2027" in job.title and len(job.description.split()) > 15
    assert job.source_status == "open" and job.url.startswith("https://")
    if source == "lever":
        assert job.published_at is None  # createdAt is never publication evidence
        assert job.updated_at is not None
    else:
        assert job.published_at and job.publication_basis != "Posting date unknown"


def test_greenhouse_updated_is_not_first_publication():
    client = FixtureClient("greenhouse")
    client.payload["detail"].pop("first_published")
    job = fetch_board(Employer("Fixture", "greenhouse", "fixture", "https://example.com"), client).jobs[0]
    assert job.published_at is None and job.updated_at is not None


def test_source_failure_is_not_an_empty_success():
    class Broken:
        def json(self, u):
            raise SourceError("HTTP 503")
    with pytest.raises(SourceError):
        fetch_board(Employer("Fixture", "ashby", "fixture", "https://example.com"), Broken())


@pytest.mark.parametrize("url,source,board", [
    ("https://job-boards.greenhouse.io/fixture", "greenhouse", "fixture"),
    ("https://boards.greenhouse.io/embed/job_board?for=fixture", "greenhouse", "fixture"),
    ("https://jobs.lever.co/fixture", "lever", "fixture"),
    ("https://jobs.ashbyhq.com/fixture", "ashby", "fixture"),
    ("https://careers.smartrecruiters.com/Fixture", "smartrecruiters", "Fixture"),
])
def test_manual_board_addition(url, source, board):
    result = employer_from_url("Fixture", url)
    assert result.source == source and result.board == board


def test_unrecognized_career_source_is_not_scraped():
    with pytest.raises(ValueError, match="manual bookmarks"):
        employer_from_url("Other", "https://example.com/careers")


class Response:
    def __init__(self, status, value):
        self.status_code, self.raw = status, value if isinstance(value, bytes) else json.dumps(value).encode()
        self.headers = {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, n):
        yield self.raw


def test_http_cache_robots_and_no_extra_calls(tmp_path):
    class Session:
        def __init__(self): self.calls = []
        def get(self, url, **kwargs):
            self.calls.append(url)
            assert kwargs["allow_redirects"] is False
            return Response(404, b"") if url.endswith("robots.txt") else Response(200, {"jobs": []})
    session = Session()
    store = Store(tmp_path / "db.sqlite3")
    client = PublicClient(store, session=session, interval=0)
    a = client.json("https://api.ashbyhq.com/posting-api/job-board/fixture")
    b = client.json("https://api.ashbyhq.com/posting-api/job-board/fixture")
    assert len(session.calls) == 2 and b[2] and a[1] == b[1]
    # Cache survives a fresh client/process; dates retain the original check.
    client2 = PublicClient(Store(store.path), session=session, interval=0)
    assert client2.json("https://api.ashbyhq.com/posting-api/job-board/fixture")[2]
    assert len(session.calls) == 2


def test_robots_disallow_prevents_api_request(tmp_path):
    class Session:
        def get(self, url, **kwargs):
            assert url.endswith("robots.txt")
            return Response(200, b"User-agent: *\nDisallow: /posting-api/")
    client = PublicClient(Store(tmp_path / "db.sqlite3"), session=Session())
    with pytest.raises(SourceError, match="disallows"):
        client.json("https://api.ashbyhq.com/posting-api/job-board/fixture")


def test_paginated_sources_read_all_pages():
    class Client:
        def __init__(self): self.calls = 0
        def json(self, url):
            self.calls += 1
            rows = [{"id": str(i), "text": "Unrelated full time"} for i in range(100)] if self.calls == 1 else [{"id": "last", "text": "Unrelated full time"}]
            return rows, NOW, False
    client = Client()
    snapshot = fetch_board(Employer("Fixture", "lever", "fixture", "https://example.com"), client)
    assert client.calls == 2 and len(snapshot.all_ids) == 101 and snapshot.complete
