from pathlib import Path

from cofounder_kernel.self_knowledge.drift import check_self_knowledge_text


def test_check_reports_missing_file_path_only_in_hand_written_sections(tmp_path: Path) -> None:
    doc = (
        "Hand pointer: `docs/missing.md`.\n"
        "<!-- AUTO-START: capabilities -->\n"
        "Generated pointer: `docs/generated-missing.md`.\n"
        "<!-- AUTO-END: capabilities -->\n"
    )

    findings = check_self_knowledge_text(doc, repo_root=tmp_path, snapshots={})

    assert [(finding.kind, finding.reference, finding.location_in_doc) for finding in findings] == [
        ("file_path", "docs/missing.md", "line 1")
    ]


def test_check_accepts_existing_file_path_and_ast_symbol(tmp_path: Path) -> None:
    module_path = tmp_path / "src" / "example" / "worker.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        return 'ok'\n",
        encoding="utf-8",
    )
    doc = "Pointers: `src/example/worker.py` and `example.worker.Worker.run`.\n"

    assert check_self_knowledge_text(doc, repo_root=tmp_path, snapshots={}) == []


def test_check_reports_missing_ast_symbol(tmp_path: Path) -> None:
    module_path = tmp_path / "src" / "example" / "worker.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("class Worker:\n    pass\n", encoding="utf-8")
    doc = "Pointer: `example.worker.Worker.run`.\n"

    findings = check_self_knowledge_text(doc, repo_root=tmp_path, snapshots={})

    assert len(findings) == 1
    assert findings[0].kind == "qualified_symbol"
    assert findings[0].reference == "example.worker.Worker.run"
    assert findings[0].reason == "No matching Python module, class, function, or method was found in src/."


def test_check_respects_reference_allowlist(tmp_path: Path) -> None:
    allowlist = tmp_path / ".zade-allowlist.txt"
    allowlist.write_text("file_path:docs/missing.md\n", encoding="utf-8")

    findings = check_self_knowledge_text(
        "Known external pointer: `docs/missing.md`.\n",
        repo_root=tmp_path,
        allowlist_path=allowlist,
        snapshots={},
    )

    assert findings == []


def test_check_validates_tool_action_and_integration_names_from_snapshots(tmp_path: Path) -> None:
    doc = "Uses `memory.search`, `local.noop`, `Ollama`, `missing.tool`, and `Missing Service`.\n"
    snapshots = {
        "capabilities": [{"name": "memory.search"}],
        "action-handlers": [{"action": "local.noop"}],
        "integrations": [{"name": "Ollama"}],
    }

    findings = check_self_knowledge_text(doc, repo_root=tmp_path, snapshots=snapshots)

    assert [(finding.kind, finding.reference) for finding in findings] == [
        ("tool", "missing.tool"),
        ("integration", "Missing Service"),
    ]
