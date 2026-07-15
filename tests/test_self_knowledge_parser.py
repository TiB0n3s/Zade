from cofounder_kernel.self_knowledge.parser import parse_auto_blocks, render_auto_blocks


def test_parse_serialize_round_trip_preserves_mixed_line_endings() -> None:
    text = (
        "# Zade\r\n"
        "Hand-written intro.\n"
        "<!-- AUTO-START: capabilities -->\r\n"
        "(generated)\n"
        "<!-- AUTO-END: capabilities -->\r\n"
        "Hand-written footer.\n"
    )

    document = parse_auto_blocks(text)

    assert document.serialize() == text
    assert [block.name for block in document.blocks] == ["capabilities"]


def test_render_auto_blocks_preserves_hand_written_sections_between_blocks() -> None:
    text = (
        "# Zade\n"
        "\n"
        "Identity stays hand-written.\n"
        "\n"
        "<!-- AUTO-START: capabilities -->\n"
        "old capabilities\n"
        "<!-- AUTO-END: capabilities -->\n"
        "\n"
        "This prose must survive unchanged.\n"
        "\n"
        "<!-- AUTO-START: integrations -->\r\n"
        "old integrations\r\n"
        "<!-- AUTO-END: integrations -->\r\n"
        "\n"
        "Pointers stay hand-written.\n"
    )

    rendered = render_auto_blocks(
        text,
        {
            "capabilities": "- memory.write\n- memory.search",
            "integrations": "- Ollama\n- SQLite",
        },
    )

    assert rendered == (
        "# Zade\n"
        "\n"
        "Identity stays hand-written.\n"
        "\n"
        "<!-- AUTO-START: capabilities -->\n"
        "- memory.write\n"
        "- memory.search\n"
        "<!-- AUTO-END: capabilities -->\n"
        "\n"
        "This prose must survive unchanged.\n"
        "\n"
        "<!-- AUTO-START: integrations -->\r\n"
        "- Ollama\r\n"
        "- SQLite\r\n"
        "<!-- AUTO-END: integrations -->\r\n"
        "\n"
        "Pointers stay hand-written.\n"
    )


def test_render_auto_blocks_leaves_unknown_blocks_unchanged() -> None:
    text = (
        "<!-- AUTO-START: capabilities -->\n"
        "old capabilities\n"
        "<!-- AUTO-END: capabilities -->\n"
        "\n"
        "<!-- AUTO-START: recent-activity -->\n"
        "old activity\n"
        "<!-- AUTO-END: recent-activity -->\n"
    )

    rendered = render_auto_blocks(text, {"capabilities": "new capabilities"})

    assert "new capabilities\n" in rendered
    assert "old activity\n" in rendered
