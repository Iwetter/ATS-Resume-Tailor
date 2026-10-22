"""Read-only allowlisted public APIs, robots checks, bounded retries and cache."""
from __future__ import annotations

from datetime import datetime, timezone
import time
import re
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .models import utcnow

HOSTS = {"boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com", "api.smartrecruiters.com"}
AGENT = "ResumeBenchHunter/2.0"


class SourceError(ValueError):
    pass


def supported_host(host):
    return host in HOSTS or bool(re.fullmatch(
        r"[a-z0-9][a-z0-9-]*\.(?:wd\d+\.myworkdayjobs\.com|recruitee\.com|jobs\.personio\.(?:com|de))", host or ""))


class PublicClient:
    def __init__(self, store, ttl=3600, interval=.5, session=None, sleep=time.sleep):
        self.store, self.ttl, self.interval = store, ttl, interval
        self.session = session or requests.Session()
        self.sleep = sleep
        self.last_request, self.robots = {}, {}
        self.requests = self.cache_hits = 0

    def _get(self, url, *, max_bytes=24*1024*1024, accept="application/json,text/plain"):
        host = urlsplit(url).hostname
        delay = self.interval - (time.monotonic()-self.last_request.get(host, 0))
        if delay > 0:
            self.sleep(delay)
        self.last_request[host] = time.monotonic()
        self.requests += 1
        try:
            with self.session.get(url, headers={"User-Agent": AGENT, "Accept": accept}, timeout=(5, 20), allow_redirects=False, stream=True) as response:
                if response.status_code >= 400 or response.status_code == 204:
                    return response.status_code, b"", response.headers
                size, chunks = 0, []
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        raise SourceError("Source response exceeds the bounded download size.")
                    chunks.append(chunk)
                return response.status_code, b"".join(chunks), response.headers
        except requests.RequestException as exc:
            raise SourceError("Network timeout or connection failure; existing job status was retained.") from exc

    def allowed(self, url):
        parts = urlsplit(url)
        host = parts.hostname
        if not supported_host(host) or parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}:
            raise SourceError("Only supported public ATS hosts over HTTPS are supported.")
        if host.endswith(".myworkdayjobs.com") and ("/wday/" in parts.path.lower() or "/cxs/" in parts.path.lower()):
            raise SourceError("Workday scanning uses public sitemaps and job pages only.")
        if host not in self.robots:
            key = "robots:" + host
            cached = self.store.cache_get(key)
            if cached and (datetime.now(timezone.utc)-datetime.fromisoformat(cached[1])).total_seconds() < 86400:
                status, text = cached[0]["status"], cached[0]["text"]
            else:
                status, raw, _ = self._get("https://" + host + "/robots.txt", max_bytes=512000)
                text = raw.decode("utf-8", errors="replace")
                if status not in {200, 404, 410}:
                    raise SourceError(f"robots.txt could not be confirmed (HTTP {status}); source skipped.")
                self.store.cache_set(key, {"status": status, "text": text}, utcnow())
            parser = RobotFileParser()
            parser.parse(text.splitlines() if status == 200 else ["User-agent: *", "Allow: /"])
            self.robots[host] = parser
        if not self.robots[host].can_fetch(AGENT, url):
            raise SourceError("robots.txt disallows this public path; source skipped.")
        crawl_delay = self.robots[host].crawl_delay(AGENT)
        if crawl_delay:
            if float(crawl_delay) > 30:
                raise SourceError("Source requests a long crawl delay; use its career page manually.")
            self.interval = max(self.interval, float(crawl_delay))

    def _read(self, url, force=False, as_text=False):
        self.allowed(url)
        cache_key = "text:" + url if as_text else url
        cached = self.store.cache_get(cache_key)
        if not force and cached and (datetime.now(timezone.utc)-datetime.fromisoformat(cached[1])).total_seconds() < self.ttl:
            self.cache_hits += 1
            return cached[0], cached[1], True
        for attempt in range(2):
            try:
                status, raw, headers = self._get(url, accept="text/html,application/xml,text/xml;q=0.9,*/*;q=0.5" if as_text else "application/json,text/plain")
            except SourceError:
                if attempt == 0:
                    self.sleep(1)
                    continue
                raise
            if status in {429, 502, 503, 504} and attempt == 0:
                retry = headers.get("Retry-After", "1")
                try:
                    delay = float(retry)
                except ValueError:
                    raise SourceError(f"Source rate-limited requests (HTTP {status}); try a later scan.")
                if delay > 5:
                    raise SourceError(f"Source requested a {delay:g}s retry delay; try a later scan.")
                self.sleep(max(1, delay))
                continue
            if status != 200:
                raise SourceError(f"Public API returned HTTP {status}; no closed status inferred from this failure.")
            if as_text:
                value = raw.decode("utf-8-sig", errors="replace")
            else:
                import json
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise SourceError("Source did not return valid JSON.") from exc
            checked = utcnow()
            self.store.cache_set(cache_key, value, checked)
            return value, checked, False
        raise SourceError("Source request failed after one bounded retry.")

    def json(self, url, force=False):
        return self._read(url, force)

    def text(self, url, force=False):
        return self._read(url, force, as_text=True)
