"""Local, terminal-only management of platform API keys in Windows Credential Manager.

Only for a platform that issues YOU an API key for a documented API. Website passwords are never stored here:
sign in yourself in the dedicated browser window from the dashboard's Connections tab.

  py -3 microtask_monitor/connect.py list
  py -3 microtask_monitor/connect.py set-key <platform_id> <key_name>     (asks with a hidden prompt)
  py -3 microtask_monitor/connect.py set-key-from-clipboard <platform_id> <key_name>
        (copy the secret first; it is read from the clipboard, stored, and the clipboard is cleared. Use this when pasting into the hidden prompt does not work.)
  py -3 microtask_monitor/connect.py delete-key <platform_id> <key_name>

The value is typed into a hidden prompt, goes straight to Credential Manager and is never printed, logged or sent anywhere.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from microtask_monitor import policy, secret_store  # noqa: E402


def read_clipboard() -> str:
    """Windows clipboard text via PowerShell (no extra package). The value is returned to the caller only, never printed."""
    import subprocess
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], capture_output=True, text=True, timeout=20)
    return (r.stdout or "").strip()


def clear_clipboard() -> None:
    import subprocess
    subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ' '"], capture_output=True, timeout=20)


def main(argv: list[str], store=None, ask=getpass.getpass, out=print, clipboard=None, clear=None) -> int:
    if argv and argv[0] == "set-key-from-clipboard":
        value_source = clipboard or read_clipboard
        argv = ["set-key", *argv[1:]]
        ask = lambda prompt: value_source()          # noqa: E731
        clear = clear or clear_clipboard
    if not argv or argv[0] not in ("list", "set-key", "delete-key"):
        out(__doc__)
        return 2
    try:
        store = store or secret_store.default_store()
    except secret_store.SecretStoreUnavailable as exc:
        out(str(exc))
        return 1
    caps = policy.load_capabilities()
    if argv[0] == "list":
        names = store.names()
        out("\n".join(f"{p}/{n}  (value hidden)" for p, n in names) or "No keys stored.")
        return 0
    if len(argv) != 3:
        out(__doc__)
        return 2
    pid, name = argv[1], argv[2]
    owner = pid in ("owner_whop", "owner_gmail", "owner_youtube")   # YOUR OWN business accounts (owner lane), not worker platforms
    if pid not in caps and not owner:
        out(f"Unknown platform '{pid}'")
        return 2
    if argv[0] == "delete-key":
        out("Deleted." if store.delete(pid, name) else "Nothing stored under that name.")
        return 0
    if not owner and caps[pid].get("access_class") != "official_api_permitted":
        out(f"{pid} has no documented API that uses a key ({caps[pid].get('access_class')}). Nothing stored.")
        return 1
    value = ask(f"Paste the API key for {pid}/{name} (hidden): ")
    if not value.strip():
        out("Empty value: nothing stored.")
        return 1
    if pid == "owner_gmail":
        value = value.replace(" ", "")               # Google shows app passwords in groups of four
    store.set(pid, name, value.strip())
    if clear:
        clear()
    out(f"Stored in Windows Credential Manager as {secret_store.target(pid, name)} ({len(value.strip())} characters). The value was not displayed or logged."
        + (" The clipboard was cleared." if clear else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
