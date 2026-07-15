from pathlib import Path

from cofounder_kernel.self_knowledge.prompt import render_prompt_self_knowledge


def test_slim_prompt_uses_identity_principles_and_capability_names_only(tmp_path: Path) -> None:
    doc_path = tmp_path / "zade.md"
    doc_path.write_text(
        "# Zade\n"
        "\n"
        "Intro outside sections should stay out of the slim prompt.\n"
        "\n"
        "## Identity\n"
        "\n"
        "Zade is a context-rich, truth-seeking co-founder.\n"
        "\n"
        "## Core Principles\n"
        "\n"
        "- Ground self-descriptions in current code.\n"
        "- Challenge weak reasoning directly.\n"
        "\n"
        "## Capabilities At A Glance\n"
        "\n"
        "<!-- AUTO-START: capabilities -->\n"
        "| Name | Category | Permission | Description |\n"
        "| --- | --- | --- | --- |\n"
        "| `memory.search` | memory | `L0_READ` | Search local memory using SQLite FTS. |\n"
        "<!-- AUTO-END: capabilities -->\n"
        "\n"
        "## Approved Action Handlers\n"
        "\n"
        "<!-- AUTO-START: action-handlers -->\n"
        "| Action | Category | Enabled | Description |\n"
        "| --- | --- | --- | --- |\n"
        "| `local.noop` | local | yes | Record a successful no-op dispatch. |\n"
        "<!-- AUTO-END: action-handlers -->\n",
        encoding="utf-8",
    )

    prompt = render_prompt_self_knowledge(doc_path=doc_path, mode="slim")

    assert "Zade is a context-rich, truth-seeking co-founder." in prompt
    assert "Ground self-descriptions in current code." in prompt
    assert "Challenge weak reasoning directly." in prompt
    assert "Capabilities: memory.search" in prompt
    assert "Approved actions: local.noop" in prompt
    assert "Search local memory using SQLite FTS" not in prompt
    assert "Intro outside sections" not in prompt


def test_full_prompt_returns_entire_doc(tmp_path: Path) -> None:
    doc_path = tmp_path / "zade.md"
    doc_path.write_text(
        "# Zade\n"
        "\n"
        "## Identity\n"
        "Identity text.\n"
        "\n"
        "## Open Questions / Unknowns\n"
        "- Full-only unknown.\n",
        encoding="utf-8",
    )

    prompt = render_prompt_self_knowledge(doc_path=doc_path, mode="full")

    assert prompt.startswith("# Zade")
    assert "Full-only unknown." in prompt


def test_missing_doc_returns_empty_prompt(tmp_path: Path) -> None:
    assert render_prompt_self_knowledge(doc_path=tmp_path / "missing.md", mode="slim") == ""
