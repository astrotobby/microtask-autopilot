"""Encrypted, offline-safe migration bundles for moving the local engine to its cloud host.

Export only operating state and deliverables. Browser profiles, cookies, Windows Credential Manager,
machine secrets, caches, sample runs and source-control state are intentionally excluded.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAGIC = b"MAPCLOUD1"
SALT_BYTES = 16
PBKDF2_ITERATIONS = 600_000
MAX_UPLOAD_BYTES = 150 * 1024 * 1024
MAX_EXPANDED_BYTES = 500 * 1024 * 1024
MAX_FILES = 10_000

DATA_FILES = (
    "latest_report.json",
    "outreach_config.json",
    "owner_config.json",
    "business_config.json",
    "settings.json",
)
DATA_DIRS = ("packets", "whop", "pilot", "executions", "evidence", "manual", "downloads")


class CloudExportError(ValueError):
    pass


def _fernet(passphrase: str, salt: bytes):
    if len(passphrase) < 12:
        raise CloudExportError("Use a migration passphrase of at least 12 characters")
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        import base64
    except ImportError as exc:
        raise CloudExportError("Install requirements.txt before creating or restoring a migration bundle") from exc
    derive = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS)
    return Fernet(base64.urlsafe_b64encode(derive.derive(passphrase.encode("utf-8"))))


def encrypt_zip(zip_bytes: bytes, passphrase: str, salt: bytes | None = None) -> bytes:
    salt = salt or os.urandom(SALT_BYTES)
    return MAGIC + salt + _fernet(passphrase, salt).encrypt(zip_bytes)


def decrypt_bundle(bundle: bytes, passphrase: str) -> bytes:
    if not isinstance(bundle, (bytes, bytearray)) or len(bundle) > MAX_UPLOAD_BYTES:
        raise CloudExportError("Migration upload is missing or exceeds the 150 MB encrypted upload limit")
    if not bundle.startswith(MAGIC) or len(bundle) <= len(MAGIC) + SALT_BYTES:
        raise CloudExportError("This is not a Microtask Autopilot cloud migration bundle")
    salt = bundle[len(MAGIC):len(MAGIC) + SALT_BYTES]
    try:
        return _fernet(passphrase, salt).decrypt(bundle[len(MAGIC) + SALT_BYTES:])
    except CloudExportError:
        raise
    except Exception as exc:
        raise CloudExportError("Migration bundle could not be decrypted (wrong passphrase or damaged file)") from exc


def _backup_database(src: Path, dest: Path) -> None:
    if not src.is_file():
        raise CloudExportError(f"Local database is missing: {src}")
    source = sqlite3.connect(str(src), timeout=30)
    target = sqlite3.connect(str(dest), timeout=30)
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise CloudExportError("Local database integrity check failed; export cancelled")
    finally:
        target.close()
        source.close()


def _copy_regular(src: Path, dst: Path) -> None:
    if src.is_symlink() or not src.is_file():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def build_bundle(root: Path, output: Path, passphrase: str) -> dict:
    root = Path(root).resolve()
    data = root / "data"
    staging = Path(tempfile.mkdtemp(prefix="microtask-cloud-export-"))
    try:
        staged_data = Path(staging) / "data"
        staged_data.mkdir(parents=True)
        _backup_database(data / "autopilot.sqlite3", staged_data / "autopilot.sqlite3")

        included = ["data/autopilot.sqlite3"]
        for name in DATA_FILES:
            source = data / name
            if source.is_file() and not source.is_symlink():
                _copy_regular(source, staged_data / name)
                included.append(f"data/{name}")
        for name in DATA_DIRS:
            source = data / name
            if source.is_dir() and not source.is_symlink():
                for file in source.rglob("*"):
                    if file.is_symlink() or not file.is_file():
                        continue
                    if any(part in {"__pycache__", ".git"} for part in file.relative_to(source).parts):
                        continue
                    rel = file.relative_to(data)
                    _copy_regular(file, staged_data / rel)
                    included.append("data/" + rel.as_posix())

        profile = root / "microtask_monitor" / "profile.json"
        if profile.is_file() and not profile.is_symlink():
            private = Path(staging) / "private"
            private.mkdir(exist_ok=True)
            _copy_regular(profile, private / "profile.json")
            included.append("private/profile.json")

        records = []
        total = 0
        for name in sorted(set(included)):
            p = Path(staging) / name
            raw = p.read_bytes()
            total += len(raw)
            records.append({"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        manifest = {
            "format": "microtask-autopilot-cloud-migration",
            "version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "database_integrity": "ok",
            "file_count": len(records),
            "uncompressed_bytes": total,
            "included": records,
            "excluded": ["data/browser-profiles", "Windows Credential Manager", "cloud/local encrypted secret files",
                         "cache", "sample-runs", ".env", "Git history", "machine credentials"],
        }
        (Path(staging) / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        zip_path = Path(staging) / "bundle.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in sorted(set(included) | {"manifest.json"}):
                archive.write(Path(staging) / name, arcname=name)
        encrypted = encrypt_zip(zip_path.read_bytes(), passphrase)
        if len(encrypted) > MAX_UPLOAD_BYTES:
            raise CloudExportError("Encrypted migration archive exceeds 150 MB; export only necessary working data")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temp_out = output.with_name(output.name + ".tmp")
        try:
            temp_out.write_bytes(encrypted)
            try:
                os.chmod(temp_out, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(temp_out, output)
        finally:
            temp_out.unlink(missing_ok=True)
        return {"path": str(output), "encrypted_bytes": len(encrypted), "file_count": len(records),
                "database_integrity": "ok", "included_profile": profile.is_file()}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def unpack_to_staging(bundle: bytes, passphrase: str, staging: Path) -> dict:
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    zip_bytes = decrypt_bundle(bundle, passphrase)
    zip_path = staging / "decrypted.zip"
    zip_path.write_bytes(zip_bytes)
    allowed_roots = {"data", "private"}
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise CloudExportError("Migration archive contains too many files")
            for info in infos:
                name = info.filename
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                    raise CloudExportError("Unsafe file path in migration bundle")
                if name in seen:
                    raise CloudExportError("Duplicate path in migration bundle")
                seen.add(name)
                if name != "manifest.json" and pure.parts[0] not in allowed_roots:
                    raise CloudExportError("Migration bundle contains an unsupported root folder")
                if pure.parts[0] == "private" and pure.parts != ("private", "profile.json"):
                    raise CloudExportError("Only the private profile.json file may be imported")
                if pure.parts[0] == "data":
                    rel = pure.parts[1:]
                    allowed_file = len(rel) == 1 and rel[0] in ({"autopilot.sqlite3"} | set(DATA_FILES))
                    allowed_dir = len(rel) >= 2 and rel[0] in set(DATA_DIRS)
                    if not (allowed_file or allowed_dir):
                        raise CloudExportError("Migration archive contains a disallowed data path")
                if info.external_attr >> 16 & 0o170000 == 0o120000:
                    raise CloudExportError("Symbolic links are not allowed in migration bundles")
                total += info.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise CloudExportError("Migration bundle expands beyond the 500 MB limit")
            archive.extractall(staging, members=infos)

        manifest_path = staging / "manifest.json"
        if not manifest_path.is_file():
            raise CloudExportError("Migration manifest is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format") != "microtask-autopilot-cloud-migration" or manifest.get("version") != 1:
            raise CloudExportError("Unsupported migration format/version")
        records = manifest.get("included")
        if not isinstance(records, list) or len(records) != manifest.get("file_count"):
            raise CloudExportError("Migration manifest file list is inconsistent")
        for record in records:
            name = record.get("path", "")
            pure = PurePosixPath(name) if isinstance(name, str) else PurePosixPath(".")
            if (name == "manifest.json" or name not in seen or pure.is_absolute() or ".." in pure.parts or
                    not pure.parts or pure.parts[0] not in allowed_roots):
                raise CloudExportError("Migration manifest contains a disallowed or missing path")
            if pure.parts[0] == "private" and pure.parts != ("private", "profile.json"):
                raise CloudExportError("Only the private profile.json file may be imported")
            if pure.parts[0] == "data":
                rel = pure.parts[1:]
                allowed_file = len(rel) == 1 and rel[0] in ({"autopilot.sqlite3"} | set(DATA_FILES))
                allowed_dir = len(rel) >= 2 and rel[0] in set(DATA_DIRS)
                if not (allowed_file or allowed_dir):
                    raise CloudExportError("Migration manifest lists a disallowed data path")
            p = staging.joinpath(*pure.parts)
            if not p.is_file() or p.is_symlink():
                raise CloudExportError("A file listed in the manifest is missing")
            raw = p.read_bytes()
            if len(raw) != int(record.get("size", -1)) or hashlib.sha256(raw).hexdigest() != record.get("sha256"):
                raise CloudExportError("A migration file failed its integrity check")
        dbfile = staging / "data" / "autopilot.sqlite3"
        if not dbfile.is_file():
            raise CloudExportError("The migrated SQLite database is missing")
        conn = sqlite3.connect(str(dbfile))
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise CloudExportError("The migrated SQLite database did not pass its integrity check")
        finally:
            conn.close()
        return manifest
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        if isinstance(exc, CloudExportError):
            raise
        raise CloudExportError(f"Migration archive validation failed: {str(exc)[:160]}") from exc
    finally:
        zip_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an encrypted cloud migration bundle (never exports browser sessions or Windows secrets).")
    parser.add_argument("--output", default="", help="output path; default data/cloud-migration.enc")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output) if args.output else root / "data" / "cloud-migration.enc"
    first = getpass.getpass("Create a private migration passphrase (12+ characters): ")
    second = getpass.getpass("Confirm migration passphrase: ")
    if first != second:
        raise SystemExit("Passphrases did not match; no archive created.")
    result = build_bundle(root, output, first)
    print(json.dumps({k: v for k, v in result.items() if k != "path"} | {"path": result["path"]}, indent=2))
    print("Keep the passphrase private. Do not send it by chat or email.")


if __name__ == "__main__":
    main()
