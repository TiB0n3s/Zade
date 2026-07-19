from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_intake_watcher_uses_events_startup_scan_and_authenticated_route() -> None:
    script = (ROOT / "scripts" / "run-project-intake-watcher.ps1").read_text(encoding="utf-8")

    assert "System.IO.FileSystemWatcher" in script
    assert r"C:\AI Brain\project-intake" in script
    assert "$Watcher.IncludeSubdirectories = $true" in script
    assert '"$BaseUrl/project-intake/scan"' in script
    assert "X-Zade-Token" in script
    assert "Invoke-ProjectIntakeScan" in script
    assert script.index("Invoke-ProjectIntakeScan") < script.index("while ($true)")
    assert "project-intake-watcher.jsonl" in script
    assert "DebounceSeconds" in script
    assert "ScanTimeoutSeconds" in script
    assert "-TimeoutSec $ScanTimeoutSeconds" in script


def test_project_intake_watcher_installer_runs_limited_at_logon() -> None:
    script = (ROOT / "scripts" / "install-project-intake-watcher-task.ps1").read_text(encoding="utf-8")

    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "run-project-intake-watcher.ps1" in script
    assert "Zade Project Intake Watcher" in script
    assert "-RunLevel Limited" in script
    assert "-WindowStyle Hidden" in script
    assert "-ErrorAction Stop" in script
