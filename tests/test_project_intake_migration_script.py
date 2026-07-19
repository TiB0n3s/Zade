from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_initial_project_migration_is_literal_recoverable_and_dry_runnable() -> None:
    script = (ROOT / "scripts" / "migrate-initial-project-intake.ps1").read_text(encoding="utf-8")

    assert "[switch]$WhatIf" in script
    assert r"C:\BookCatalogingApp" in script
    assert r"C:\AI Brain\.trash\dark-index-legacy" in script
    assert 'Join-Path $IntakeRoot "The Dark Index"' in script
    assert 'Join-Path $IntakeRoot "Same Ground"' in script
    assert "Move-Item -LiteralPath" in script
    assert "Remove-Item" not in script
    assert "Get-FileHash -LiteralPath" in script
    assert "Test-Path -LiteralPath" in script
    assert "migration-receipt" in script
    assert "$Candidates = @(@(" in script
    assert script.index("Move-Item -LiteralPath $LegacyDarkIndex") < script.index("New-Item -ItemType Directory -Path $DarkIndexDestination")


def test_initial_project_migration_names_only_founder_source_material() -> None:
    script = (ROOT / "scripts" / "migrate-initial-project-intake.ps1").read_text(encoding="utf-8")

    for name in (
        "dark_index_zade_context_pack.md",
        "dark_index_project_workbook.xlsx",
        "Same_Ground_Zade_Handoff.md",
        "Same_Ground_Project_Workbook.xlsx",
        "Same_Ground_CSV_Bundle.zip",
    ):
        assert name in script
    assert "package.json" not in script
    assert "package-lock.json" not in script
    assert "node_modules" not in script
