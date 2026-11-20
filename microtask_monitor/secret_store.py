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
from pathlib import Path

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


class EncryptedFileStore:
    """Encrypted cloud secret vault for one persistent service instance.

    The Fernet key is supplied by the hosting provider as AUTOPILOT_SECRET_ENCRYPTION_KEY.
    Rotated OAuth tokens are encrypted at rest under the persistent data directory. Environment
    secrets are accepted as bootstrap values; encrypted-file values take precedence after rotation.
    """
    ENV_PREFIX = "AUTOPILOT_SECRET__"

    def __init__(self, path=None, key: str | None = None):
        root = Path(os.environ.get("AUTOPILOT_DATA_DIR") or (Path(__file__).resolve().parents[1] / "data"))
        self.path = Path(path or os.environ.get("AUTOPILOT_SECRETS_FILE") or (root / "secrets.enc"))
        self.key = key if key is not None else os.environ.get("AUTOPILOT_SECRET_ENCRYPTION_KEY", "")

    @staticmethod
    def _env_name(platform_id: str, name: str) -> str:
        target(platform_id, name)
        return EncryptedFileStore.ENV_PREFIX + platform_id.upper() + "__" + name.upper()

    def _fernet(self):
        if not self.key:
            raise SecretStoreUnavailable("Set AUTOPILOT_SECRET_ENCRYPTION_KEY in the cloud host before storing or rotating account tokens")
        try:
            from cryptography.fernet import Fernet
            return Fernet(self.key.encode("ascii"))
        except ImportError as exc:
            raise SecretStoreUnavailable("Cloud secret encryption dependency is missing") from exc
        except Exception as exc:
            raise SecretStoreUnavailable("AUTOPILOT_SECRET_ENCRYPTION_KEY must be a valid Fernet key") from exc

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = self._fernet().decrypt(self.path.read_bytes())
            value = __import__("json").loads(raw.decode("utf-8"))
            if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                raise ValueError("invalid secret map")
            return value
        except SecretStoreUnavailable:
            raise
        except Exception as exc:
            raise SecretStoreUnavailable("Encrypted cloud secret file could not be decrypted; refusing to overwrite it") from exc

    def _save(self, values: dict[str, str]) -> None:
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = self._fernet().encrypt(json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(temp, "wb") as f:
                f.write(token)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, platform_id: str, name: str) -> str | None:
        key = target(platform_id, name)
        values = self._load()
        if key in values:
            return values[key]
        return os.environ.get(self._env_name(platform_id, name)) or None

    def set(self, platform_id: str, name: str, value: str) -> None:
        if not value:
            raise ValueError("empty secret")
        key = target(platform_id, name)
        values = self._load()
        values[key] = str(value)
        self._save(values)

    def delete(self, platform_id: str, name: str) -> bool:
        key = target(platform_id, name)
        values = self._load()
        found = key in values or self._env_name(platform_id, name) in os.environ
        if key in values:
            values.pop(key, None)
            if values:
                self._save(values)
            else:
                self.path.unlink(missing_ok=True)
        os.environ.pop(self._env_name(platform_id, name), None)
        return found

    def names(self) -> list[tuple[str, str]]:
        values = self._load()
        found = {tuple(k[len(PREFIX):].split("/", 1)) for k in values if k.startswith(PREFIX) and "/" in k}
        for key in os.environ:
            if key.startswith(self.ENV_PREFIX):
                rest = key[len(self.ENV_PREFIX):].lower()
                if "__" in rest:
                    found.add(tuple(rest.split("__", 1)))
        return sorted(found)


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
    if os.environ.get("AUTOPILOT_RUNTIME", "").lower() == "cloud":
        return EncryptedFileStore()
    return WindowsCredentialStore()


# ------------------------------------------------------------------------------------------- long secrets (OAuth tokens)
CHUNK = 1000          # Credential Manager caps one blob at 2560 bytes (1280 UTF-16 characters); long tokens are split


def put_long(store, platform_id: str, name: str, value: str) -> None:
    """Store a secret of any length as numbered parts. Old parts beyond the new length are removed."""
    if not value:
        raise ValueError("empty secret")
    parts = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)]
    for i, part in enumerate(parts):
        store.set(platform_id, f"{name}_{i}", part)
    i = len(parts)
    while store.delete(platform_id, f"{name}_{i}"):
        i += 1


def get_long(store, platform_id: str, name: str) -> str | None:
    out, i = [], 0
    while True:
        part = store.get(platform_id, f"{name}_{i}")
        if part is None:
            break
        out.append(part)
        i += 1
    return "".join(out) or None


def delete_long(store, platform_id: str, name: str) -> bool:
    i = 0
    while store.delete(platform_id, f"{name}_{i}"):
        i += 1
    return i > 0
