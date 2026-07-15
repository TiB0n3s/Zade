from pathlib import Path

from cofounder_kernel.self_knowledge import __main__ as self_knowledge_cli


def test_cli_render_prints_rendered_doc_without_writing(monkeypatch, tmp_path: Path, capsys) -> None:
    doc_path = tmp_path / "zade.md"
    doc_path.write_text(
        "Intro stays.\n"
        "<!-- AUTO-START: capabilities -->\nold\n<!-- AUTO-END: capabilities -->\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        self_knowledge_cli,
        "collect_snapshots",
        lambda **_: {"capabilities": [{"name": "memory.search", "description": "Search.", "permission_tier": "L0_READ"}]},
    )

    code = self_knowledge_cli.run(["--doc", str(doc_path), "--repo-root", str(tmp_path), "--render"])

    captured = capsys.readouterr()
    assert code == 0
    assert "`memory.search`" in captured.out
    assert "old\n" in doc_path.read_text(encoding="utf-8")


def test_cli_check_strict_returns_nonzero_for_drift(tmp_path: Path, capsys) -> None:
    doc_path = tmp_path / "zade.md"
    doc_path.write_text("Stale pointer: `docs/missing.md`.\n", encoding="utf-8")

    code = self_knowledge_cli.run(
        ["--doc", str(doc_path), "--repo-root", str(tmp_path), "--check", "--strict"]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "file_path" in captured.out
    assert "docs/missing.md" in captured.out


def test_cli_check_strict_passes_when_references_are_current(tmp_path: Path, capsys) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "current.md").write_text("ok\n", encoding="utf-8")
    doc_path = tmp_path / "zade.md"
    doc_path.write_text("Current pointer: `docs/current.md`.\n", encoding="utf-8")

    code = self_knowledge_cli.run(
        ["--doc", str(doc_path), "--repo-root", str(tmp_path), "--check", "--strict"]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "no drift findings" in captured.out
