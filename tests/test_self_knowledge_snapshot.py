from pathlib import Path

from cofounder_kernel.config import KernelConfig, PathConfig, VoiceConfig
from cofounder_kernel.self_knowledge.renderer import render_self_knowledge
from cofounder_kernel.self_knowledge.snapshot import collect_snapshots, refresh_doc


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        paths=PathConfig(
            hot_root=tmp_path / "hot",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        voice=VoiceConfig(stt_engine="deepgram", tts_engine="elevenlabs"),
    )


def test_collect_snapshots_reads_runtime_app_state(tmp_path: Path) -> None:
    config = _config(tmp_path)

    snapshots = collect_snapshots(config=config, repo_root=Path.cwd())

    tool_names = {item["name"] for item in snapshots["capabilities"]}
    handler_actions = {item["action"] for item in snapshots["action-handlers"]}
    integration_names = {item["name"] for item in snapshots["integrations"]}

    assert {"memory.write", "memory.search", "audit.recent"}.issubset(tool_names)
    assert {"local.noop", "local.memory.write", "dev.command.run", "local.vault.move"}.issubset(handler_actions)
    assert {"Ollama", "SQLite memory", "AI Brain hot/cold roots", "Read-only connectors"}.issubset(
        integration_names
    )
    assert snapshots["voice-loop"]["stt"]["engine"] == "deepgram"
    assert snapshots["voice-loop"]["tts"]["engine"] == "elevenlabs"
    assert snapshots["skills"]["summary"]["total"] == 0
    assert snapshots["runtime-prompt-wiring"]["prompt_builder"].endswith("RuntimeService._build_governed_prompt")
    assert isinstance(snapshots["recent-activity"], list)


def test_collect_snapshots_includes_handlers_registered_by_create_app(monkeypatch, tmp_path: Path) -> None:
    from cofounder_kernel import api as api_module

    original_create_app = api_module.create_app

    def create_app_with_extra_handler(config: KernelConfig | None = None, **kwargs):
        app = original_create_app(config, **kwargs)
        app.state.handlers.register("local.test.extra", "Extra test handler.", lambda item: {"status": "ok"})
        return app

    monkeypatch.setattr(api_module, "create_app", create_app_with_extra_handler)

    snapshots = collect_snapshots(config=_config(tmp_path), repo_root=Path.cwd())

    assert "local.test.extra" in {item["action"] for item in snapshots["action-handlers"]}


def test_real_snapshot_renders_doc_end_to_end(tmp_path: Path) -> None:
    config = KernelConfig(
        paths=PathConfig(
            hot_root=tmp_path / "hot",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        voice=VoiceConfig(stt_engine="deepgram", tts_engine="elevenlabs"),
    )
    template = Path("context/self/zade.md").read_text(encoding="utf-8")

    snapshots = collect_snapshots(config=config, repo_root=Path.cwd())
    rendered = render_self_knowledge(template, snapshots)

    assert "`memory.search`" in rendered
    assert "`local.vault.move`" in rendered
    assert "Deepgram" in rendered
    assert "Zade is a context-rich, truth-seeking co-founder." in rendered


def test_refresh_doc_writes_rendered_self_knowledge(tmp_path: Path) -> None:
    doc_path = tmp_path / "zade.md"
    doc_path.write_text(
        "Intro stays.\n"
        "<!-- AUTO-START: capabilities -->\nold\n<!-- AUTO-END: capabilities -->\n"
        "<!-- AUTO-START: recent-activity -->\nold\n<!-- AUTO-END: recent-activity -->\n",
        encoding="utf-8",
    )

    result = refresh_doc(
        doc_path=doc_path,
        config=_config(tmp_path),
        repo_root=Path.cwd(),
        snapshots={"recent-activity": [{"hash": "abc1234", "date": "2026-07-15", "subject": "Commit"}]},
    )

    text = doc_path.read_text(encoding="utf-8")
    assert result["changed"] is True
    assert "Intro stays." in text
    assert "`memory.search`" in text
    assert "`abc1234` 2026-07-15 - Commit" in text
