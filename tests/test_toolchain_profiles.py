from __future__ import annotations

import json
from pathlib import Path
import sys

from cofounder_kernel.toolchain_profiles import ToolchainRegistry


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_flutter_profile_uses_well_known_windows_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "flutter_app"
    workspace.mkdir()
    (workspace / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")
    (workspace / "lib").mkdir()
    (workspace / "android").mkdir()
    _touch(workspace / ".dart_tool" / "package_config.json")
    gradle = _touch(workspace / "android" / "gradlew.bat")

    flutter = _touch(tmp_path / "flutter" / "bin" / "flutter.bat")
    dart = _touch(tmp_path / "flutter" / "bin" / "dart.bat")
    sdk = tmp_path / "android-sdk"
    adb = _touch(sdk / "platform-tools" / "adb.exe")
    emulator = _touch(sdk / "emulator" / "emulator.exe")

    registry = ToolchainRegistry(
        env={"FLUTTER_HOME": str(flutter.parent.parent), "ANDROID_HOME": str(sdk)},
        which=lambda _name: None,
    )

    profile = registry.detect(workspace)

    assert profile.id == "flutter-mobile"
    probes = {probe.id: probe for probe in profile.probes}
    assert probes["flutter"].path == flutter.resolve()
    assert probes["dart"].path == dart.resolve()
    assert probes["gradle-wrapper"].path == gradle.resolve()
    assert probes["adb"].path == adb.resolve()
    assert probes["android-emulator"].path == emulator.resolve()
    assert [command.argv[1:] for command in profile.verification_commands] == [
        ("analyze", "--no-pub"),
        ("test", "--no-pub"),
        ("build", "apk", "--debug", "--no-pub"),
    ]
    assert all(command.required for command in profile.verification_commands)


def test_flutter_profile_reports_exact_missing_tool_blockers(tmp_path: Path) -> None:
    workspace = tmp_path / "flutter_app"
    workspace.mkdir()
    (workspace / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")
    (workspace / "lib").mkdir()

    profile = ToolchainRegistry(
        env={}, which=lambda _name: None, include_system_paths=False
    ).detect(workspace)
    blockers = profile.blockers

    assert any("flutter" in blocker.lower() for blocker in blockers)
    assert any("C:\\tools\\flutter" in blocker for blocker in blockers)
    assert any("flutter pub get" in blocker for blocker in blockers)


def test_node_profile_uses_declared_scripts_without_install_or_npx(tmp_path: Path) -> None:
    workspace = tmp_path / "node_app"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": "vitest", "typecheck": "tsc --noEmit", "test:e2e": "playwright test"},
                "devDependencies": {"typescript": "5.9.3", "@playwright/test": "1.58.0"},
            }
        ),
        encoding="utf-8",
    )
    npm = _touch(tmp_path / "node" / "npm.cmd")
    node = _touch(tmp_path / "node" / "node.exe")
    registry = ToolchainRegistry(
        env={},
        which=lambda name: {"npm": str(npm), "node": str(node)}.get(name),
        include_system_paths=False,
    )

    profile = registry.detect(workspace)
    argvs = [command.argv for command in profile.verification_commands]

    assert profile.id == "node-saas"
    assert argvs == [
        (str(npm.resolve()), "test"),
        (str(npm.resolve()), "run", "typecheck"),
        (str(npm.resolve()), "run", "test:e2e"),
    ]
    assert not any("install" in argv or "npx" in argv for command in argvs for argv in command)


def test_python_profile_uses_kernel_interpreter_and_pytest(tmp_path: Path) -> None:
    workspace = tmp_path / "python_app"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    (workspace / "tests").mkdir()
    registry = ToolchainRegistry(
        env={},
        which=lambda _name: None,
        include_system_paths=False,
    )

    profile = registry.detect(workspace)

    assert profile.id == "python-saas"
    assert profile.verification_commands[0].argv == (
        str(Path(sys.executable).resolve()),
        "-m",
        "pytest",
        "-q",
    )


def test_generic_profile_is_read_only_and_has_no_fake_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "unknown"
    workspace.mkdir()

    profile = ToolchainRegistry(
        env={}, which=lambda _name: None, include_system_paths=False
    ).detect(workspace)

    assert profile.id == "generic"
    assert profile.verification_commands == ()
    assert profile.blockers == ("No supported SaaS or mobile toolchain was detected.",)


def test_inventory_reports_all_profiles_without_exposing_environment_values(tmp_path: Path) -> None:
    registry = ToolchainRegistry(
        env={"OPENAI_API_KEY": "secret", "PATH": str(tmp_path)},
        which=lambda _name: None,
        include_system_paths=False,
    )

    inventory = registry.inventory(tmp_path)

    assert {item["id"] for item in inventory["profiles"]} == {
        "generic",
        "python-saas",
        "node-saas",
        "flutter-mobile",
    }
    assert "secret" not in repr(inventory)
