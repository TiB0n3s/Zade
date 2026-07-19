from pathlib import Path

from cofounder_kernel.config import load_config


def test_load_config_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[app]
host = "127.0.0.1"
port = 9999

[identity]
name = "Zade"
description = "Local operating partner"

[paths]
hot_root = "C:\\\\AI Brain"
cold_root = "D:\\\\AI Brain-Cold"
data_dir = "C:\\\\AI Brain\\\\memory-hot\\\\test-kernel"

[ollama]
base_url = "http://127.0.0.1:11434"
chat_model = "qwen3:14b"
embedding_model = "nomic-embed-text"
think = false
temperature = 0.1

[prompt_profiles]
default = "build"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.app.port == 9999
    assert config.identity.name == "Zade"
    assert config.identity.description == "Local operating partner"
    assert config.paths.hot_root == Path(r"C:\AI Brain")
    assert config.paths.cold_root == Path(r"D:\AI Brain-Cold")
    assert config.ollama.chat_model == "qwen3:14b"
    assert config.ollama.reasoning_model == "deepseek-r1:14b"
    assert config.ollama.coding_model == "qwen2.5-coder:14b"
    assert config.ollama.think is False
    assert config.ollama.model_for_role("general") == "qwen3:14b"
    assert config.ollama.model_for_role("reasoning") == "deepseek-r1:14b"
    assert config.ollama.model_for_role("coding") == "qwen2.5-coder:14b"
    assert config.ollama.think_for_role("general") is False
    assert config.ollama.think_for_role("reasoning") is True
    assert config.prompt_profiles.default == "build"


def test_load_config_accepts_hybrid_delegation_engine(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[delegation]\nengine = "hybrid"\n', encoding="utf-8")

    config = load_config(config_path)

    assert config.delegation.engine == "hybrid"
