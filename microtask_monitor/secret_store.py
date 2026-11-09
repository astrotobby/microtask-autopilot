"""OS-protected secret storage (Windows Credential Manager) and redaction helpers.

Secrets (API keys, if a platform ever issues one for a documented API) live ONLY in Windows Credential Manager under
``MicrotaskAutopilot/<platform>/<name>``. They are never written to SQLite, logs, reports, source files or the dashboard,
and no HTTP endpoint reads or writes them. Use ``py -3 microtask_monitor/connect.py`` (it asks with a hidden prompt).

Website passwords are NOT stored here: you sign in yourself in a dedicated browser profile, and the browser keeps that
session in its own OS-encrypted store. There is no plaintext fallback: on other operating systems storage is refused.
"""
from __future__ import annotations

import ctypes
import os
import re
from typing import Any

PREFIX = "MicrotaskAutopilot/"
NAME_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_REDACT = [
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|session|cookie|authorization|bearer|otp|mfa[_-]?code)\b(\s*[:=]\s*|\s+)([\"']?)[^\s\"',;]{3,}\3"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})\b"),
    re.compile(r"(?i)([?&](?:token|key|sig|signature|session|auth|code)=)[^&\s]+"),
]


class SecretStoreUnavailable(RuntimeError):
    pass


def redact(text: Any) -> str:
    """Remove anything that looks like a secret from text destined for logs, SQLite, notifications or the dashboard."""
    s = str(text or "")
    s = _REDACT[1].sub("Bearer [REDACTED]", s)
    s = _REDACT[0].sub(lambda m: m.group(0) if "[REDACTED]" in m.group(0) or m.group(0).rstrip().endswith("Bearer") else f"{m.group(1)}{m.group(2)}[REDACTED]", s)
    s = _REDACT[2].sub("[REDACTED]", s)
    s = _REDACT[3].sub(lambda m: m.group(1) + "[REDACTED]", s)
    return s


def target(platform_id: str, name: str) -> str:
    if not NAME_RE.match(platform_id or "") or not NAME_RE.match(name or ""):
        raise ValueError("platform and secret names must be short lower-case identifiers")
    return f"{PREFIX}{platform_id}/{name}"


# ------------------------------------------------------------------------------------------- Windows Credential Manager
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


if os.name == "nt":
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _adv.CredWriteW.restype = wintypes.BOOL
    _adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _adv.CredReadW.restype = wintypes.BOOL
    _adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _adv.CredDeleteW.restype = wintypes.BOOL
    _adv.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)))]
    _adv.CredEnumerateW.restype = wintypes.BOOL
    _adv.CredFree.argtypes = [ctypes.c_void_p]


class WindowsCredentialStore:
    """Thin ctypes wrapper over CredWriteW / CredReadW / CredDeleteW / CredEnumerateW (current user only)."""

    def __init__(self):
        if os.name != "nt":
            raise SecretStoreUnavailable("Windows Credential Manager is only available on Windows; secrets are never stored in plain files")

    def set(self, platform_id: str, name: str, value: str) -> None:
        if not value:
            raise ValueError("empty secret")
        t = target(platform_id, name)
        blob = value.encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = _CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=t, Comment="Microtask Autopilot (local only)", CredentialBlobSize=len(blob),
                           CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=name)
        if not _adv.CredWriteW(ctypes.byref(cred), 0):
            raise SecretStoreUnavailable(f"Credential Manager refused to store it (error {ctypes.get_last_error()})")

    def get(self, platform_id: str, name: str) -> str | None:
        p = ctypes.POINTER(_CREDENTIAL)()
        if not _adv.CredReadW(target(platform_id, name), CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
            if ctypes.get_last_error() == ERROR_NOT_FOUND:
                return None
            raise SecretStoreUnavailable(f"Credential Manager read failed (error {ctypes.get_last_error()})")
        try:
            c = p.contents
            return bytes(c.CredentialBlob[: c.CredentialBlobSize]).decode("utf-16-le")
        finally:
            _adv.CredFree(p)

    def delete(self, platform_id: str, name: str) -> bool:
        if _adv.CredDeleteW(target(platform_id, name), CRED_TYPE_GENERIC, 0):
            return True
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return False
        raise SecretStoreUnavailable(f"Credential Manager delete failed (error {ctypes.get_last_error()})")

    def names(self) -> list[tuple[str, str]]:
        """(platform, name) pairs that exist. Values are never returned."""
        count = wintypes.DWORD()
        arr = ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))()
        if not _adv.CredEnumerateW(PREFIX + "*", 0, ctypes.byref(count), ctypes.byref(arr)):
            return []
        try:
            out = []
            for i in range(count.value):
                rest = arr[i].contents.TargetName[len(PREFIX):]
                if "/" in rest:
                    out.append(tuple(rest.split("/", 1)))
            return sorted(out)
        finally:
            _adv.CredFree(arr)


class MemoryStore:
    """In-memory store for tests only."""

    def __init__(self):
        self._d: dict[str, str] = {}

    def set(self, platform_id, name, value):
        if not value:
            raise ValueError("empty secret")
        self._d[target(platform_id, name)] = value

    def get(self, platform_id, name):
        return self._d.get(target(platform_id, name))

    def delete(self, platform_id, name):
        return self._d.pop(target(platform_id, name), None) is not None

    def names(self):
        return sorted(tuple(k[len(PREFIX):].split("/", 1)) for k in self._d)


def default_store():
    return WindowsCredentialStore()
