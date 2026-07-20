from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_inbox_watcher_uses_filesystem_events_and_runs_the_local_work_scan() -> None:
    script = (ROOT / "scripts" / "run-inbox-watcher.ps1").read_text(encoding="utf-8")

    assert "System.IO.FileSystemWatcher" in script
    assert '"$BaseUrl/work/scan"' in script
    assert "X-Zade-Token" in script
    assert "IncludeSubdirectories" in script
    assert '"ZadeInboxCreated"' in script
    assert '"ZadeInboxChanged"' in script
    assert '"ZadeInboxRenamed"' in script


def test_inbox_watcher_installer_runs_at_user_logon() -> None:
    script = (ROOT / "scripts" / "install-inbox-watcher-task.ps1").read_text(encoding="utf-8")

    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "run-inbox-watcher.ps1" in script
    assert "Zade Inbox Watcher" in script
    assert "-ErrorAction Stop" in script
