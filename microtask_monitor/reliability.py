"""Network and file reliability helpers: backoff, resumable downloads, atomic writes.

Everything here is standard-library only. Sleeping can be disabled for tests with
``AUTOPILOT_NO_SLEEP=1`` so retry logic is exercised without real delays.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import random
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

USER_AGENT = "Microtask-Autopilot-Local-Agent/1.0"


def sleep(seconds: float) -> None:
    if os.environ.get("AUTOPILOT_NO_SLEEP") != "1":
        time.sleep(seconds)


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0, jitter: bool = True) -> float:
    """Exponential backoff: base * 2^attempt, capped, with up to 25% jitter."""
    delay = min(cap, base * (2 ** attempt))
    return delay * (1 + random.random() * 0.25) if jitter else delay


def with_retries(fn: Callable[[], Any], *, attempts: int = 4, base: float = 1.0, cap: float = 30.0,
                 retry_if: Callable[[BaseException], bool] = lambda e: True,
                 retry_after: Callable[[BaseException], float | None] = lambda e: None,
                 on_retry: Callable[[int, BaseException, float], None] | None = None) -> Any:
    """Call ``fn`` up to ``attempts`` times, backing off between retryable failures."""
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:                      # noqa: BLE001 - classified by retry_if
            last = exc
            if attempt == attempts - 1 or not retry_if(exc):
                raise
            wait = retry_after(exc)
            wait = min(cap, wait) if wait else backoff_delay(attempt, base, cap)
            if on_retry:
                on_retry(attempt + 1, exc, wait)
            sleep(wait)
    raise last  # pragma: no cover


def is_transient_network_error(exc: BaseException) -> bool:
    return isinstance(exc, (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError)) \
        and not isinstance(exc, urllib.error.HTTPError)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same folder, fsync, then replace. A crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DownloadIncomplete(RuntimeError):
    pass


def ranged_download(url: str, part_path: Path, *, expected_size: int | None = None,
                    opener: Callable[..., Any] = urllib.request.urlopen, attempts: int = 8,
                    chunk: int = 16384, timeout: int = 60, max_bytes: int = 32 * 1024 * 1024) -> bytes:
    """Download ``url`` into ``part_path`` and RESUME with HTTP Range after any interruption.

    Progress already written to the .part file is kept across retries (and across program runs).
    Returns the complete bytes. Raises DownloadIncomplete if the size cannot be reached.
    """
    part_path = Path(part_path)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: BaseException | None = None
    for attempt in range(attempts):
        existing = part_path.stat().st_size if part_path.exists() else 0
        if expected_size is not None and existing > expected_size:
            part_path.unlink()
            existing = 0
        if expected_size is not None and existing == expected_size:
            break
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                status = getattr(response, "status", 200)
                mode = "ab" if (status == 206 and existing) else "wb"
                total = existing if mode == "ab" else 0
                with open(part_path, mode) as handle:
                    while True:
                        block = response.read(chunk)
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise DownloadIncomplete("download exceeds the size limit")
                        handle.write(block)
                        handle.flush()
            if expected_size is None:
                break
        except urllib.error.HTTPError as exc:
            if exc.code == 416:                        # range not satisfiable: start over unless already complete
                if expected_size is not None and existing == expected_size:
                    break
                part_path.unlink(missing_ok=True)
            elif exc.code in (429, 500, 502, 503, 504):
                last_error = exc
            else:
                raise
        except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as exc:
            last_error = exc                           # keep the partial file and resume next loop
        if attempt < attempts - 1:
            sleep(backoff_delay(attempt, base=0.5, cap=15))
    size = part_path.stat().st_size if part_path.exists() else 0
    if expected_size is not None and size != expected_size:
        raise DownloadIncomplete(f"got {size} of {expected_size} bytes" + (f" ({last_error})" if last_error else ""))
    return part_path.read_bytes()
