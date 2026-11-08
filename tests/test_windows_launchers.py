import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherTests(unittest.TestCase):
    def test_check_runner_changes_to_its_own_folder_first(self):
        text = (ROOT / "run-windows-checks.bat").read_text(encoding="ascii")
        self.assertIn('cd /d "%~dp0"', text)
        self.assertLess(text.index('cd /d "%~dp0"'), text.index("py -3"))
        self.assertIn("unittest discover -s tests", text)
        self.assertEqual(text.count("microtask_monitor\\sync_reports.py") >= 2, True)

    def test_launchers_use_absolute_script_paths(self):
        text = (ROOT / "dashboard" / "open-dashboard.bat").read_text(encoding="ascii")
        self.assertIn("%~dp0", text)

    def test_autostart_replaces_instead_of_duplicating_task(self):
        text = (ROOT / "dashboard" / "install-autostart.bat").read_text(encoding="ascii")
        self.assertIn("-TaskName 'MicrotaskAutopilotLocalAgent'", text)
        self.assertIn("-Force", text)
        self.assertIn("-User $env:USERNAME", text)

    def test_batch_files_are_checked_out_with_crlf(self):
        attrs = (ROOT / ".gitattributes").read_text(encoding="ascii")
        self.assertIn("*.bat text eol=crlf", attrs)


if __name__ == "__main__":
    unittest.main()
