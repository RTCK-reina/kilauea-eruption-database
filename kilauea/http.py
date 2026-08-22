"""HTTP helpers built on the standard library.

Deliberately no ``requests``: this pipeline runs unattended from cron on a Mac
whose system Python is 3.9 with no third-party packages installed, and a
dependency that has to be pip-installed is a dependency that will one day be
missing when the job fires at 06:00. Everything here uses ``urllib``.
"""
from __future__ import annotations

import gzip
import hashlib
import json as _json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# www.usgs.gov sits behind CloudFront and answers non-browser user agents with
# a 403. Everything else is happy with the descriptive research UA.
_BROWSER_UA_HOSTS = ("www.usgs.gov",)

_RETRY_STATUS = {429, 500, 502, 503, 504}
_ATTEMPTS = 5
_BACKOFF = 1.5

_SSL_CTX = ssl.create_default_context()


class Response:
    """Minimal stand-in for the parts of requests.Response actually used."""

    __slots__ = ("content", "status_code", "url", "_encoding")

    def __init__(self, content: bytes, status_code: int, url: str, encoding: str):
        self.content = content
        self.status_code = status_code
        self.url = url
        self._encoding = encoding or "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode(self._encoding, errors="replace")

    def json(self):
        return _json.loads(self.text)


def _ua_for(url: str) -> str:
    return _BROWSER_UA if any(h in url for h in _BROWSER_UA_HOSTS) else config.USER_AGENT


def _charset(headers) -> str:
    ctype = headers.get("Content-Type", "") or ""
    for part in ctype.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip().strip('"')
    return "utf-8"


def _open(req: urllib.request.Request, timeout: int):
    """Issue a request, retrying transient failures with backoff."""
    last: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_STATUS or attempt == _ATTEMPTS - 1:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            if attempt == _ATTEMPTS - 1:
                raise
            last = exc
        wait = _BACKOFF * (2 ** attempt)
        log.debug("retrying %s in %.1fs (%s)", req.full_url, wait, last)
        time.sleep(wait)
    raise RuntimeError(f"unreachable: {req.full_url}")


def _read(resp) -> bytes:
    data = resp.read()
    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
        data = gzip.decompress(data)
    return data


def get(url: str, *, params=None, timeout: int = 120, headers=None) -> Response:
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    hdrs = {"User-Agent": _ua_for(url), "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    with _open(req, timeout) as resp:
        return Response(_read(resp), resp.status, resp.url, _charset(resp.headers))


def post_json(url: str, payload: dict, *, timeout: int = 120) -> dict:
    body = _json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": _ua_for(url), "Content-Type": "application/json",
                 "Accept-Encoding": "gzip"})
    with _open(req, timeout) as resp:
        return _json.loads(_read(resp).decode(_charset(resp.headers), errors="replace"))


def cached_download(url: str, name: str, *, subdir: str = "", force: bool = False) -> Path:
    """Download ``url`` into the raw cache and return the local path.

    The cache key is the file name plus a short hash of the URL, so two
    ScienceBase items that both ship ``UWD_digital.zip`` never collide.
    """
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    target_dir = config.RAW_DIR / subdir if subdir else config.RAW_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{digest}_{name}"

    if dest.exists() and dest.stat().st_size > 0 and not force:
        log.debug("cache hit %s", dest.name)
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    started = time.time()
    # No Accept-Encoding here: these are already-compressed archives, and
    # streaming them straight to disk keeps large files out of memory.
    req = urllib.request.Request(url, headers={"User-Agent": _ua_for(url)})
    with _open(req, 600) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    tmp.rename(dest)
    log.info("downloaded %s (%.1f MB in %.1fs)",
             name, dest.stat().st_size / 1e6, time.time() - started)
    return dest
