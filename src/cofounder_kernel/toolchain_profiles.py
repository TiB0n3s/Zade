"""Deterministic SaaS and mobile toolchain discovery and verification plans."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Callable, Mapping


@dataclass(frozen=True)
class ToolchainProbe:
    id: str
    available: bool
    path: Path | None
    version_argv: tuple[str, ...]
    blocker: str = ""


@dataclass(frozen=True)
class VerificationCommand:
    id: str
    argv: tuple[str, ...]
    required: bool = True
    timeout_seconds: float = 600.0
    artifact_kind: str = "log"


@dataclass(frozen=True)
class ToolchainProfile:
    id: str
    label: str
    probes: tuple[ToolchainProbe, ...]
    verification_commands: tuple[VerificationCommand, ...]
    blockers: tuple[str, ...] = ()
    docker_image: str | None = None


class ToolchainRegistry:
    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] | None = None,
        include_system_paths: bool = True,
    ):
        self.env = dict(os.environ if env is None else env)
        self.which = which or shutil.which
        self.include_system_paths = include_system_paths

    def detect(self, workspace: Path | str) -> ToolchainProfile:
        root = Path(workspace).expanduser().resolve()
        if (root / "pubspec.yaml").is_file() and (root / "lib").is_dir():
            return self._flutter_profile(root)
        if (root / "package.json").is_file():
            return self._node_profile(root)
        if (root / "pyproject.toml").is_file() or (root / "tests").is_dir():
            return self._python_profile(root)
        return self._generic_profile()

    def profile(self, profile_id: str, workspace: Path | str) -> ToolchainProfile:
        root = Path(workspace).expanduser().resolve()
        profiles = {
            "generic": self._generic_profile,
            "python-saas": lambda: self._python_profile(root),
            "node-saas": lambda: self._node_profile(root),
            "flutter-mobile": lambda: self._flutter_profile(root),
        }
        factory = profiles.get(profile_id)
        if factory is None:
            raise ValueError(f"Unknown toolchain profile: {profile_id}")
        return factory()

    def inventory(self, workspace: Path | str) -> dict[str, object]:
        root = Path(workspace).expanduser().resolve()
        detected = self.detect(root)
        items: list[dict[str, object]] = []
        for profile_id in ("generic", "python-saas", "node-saas", "flutter-mobile"):
            profile = self.profile(profile_id, root)
            items.append(
                {
                    "id": profile.id,
                    "label": profile.label,
                    "detected": profile.id == detected.id,
                    "ready": not profile.blockers,
                    "blockers": list(profile.blockers),
                    "tools": [
                        {
                            "id": probe.id,
                            "available": probe.available,
                            "path": str(probe.path) if probe.path else None,
                            "blocker": probe.blocker,
                        }
                        for probe in profile.probes
                    ],
                }
            )
        return {"detected": detected.id, "profiles": items}

    def _python_profile(self, root: Path) -> ToolchainProfile:
        python = self._tool("python", (sys.executable, "python", "python3"))
        probes = (
            ToolchainProbe(
                id="python",
                available=python is not None,
                path=python,
                version_argv=(str(python), "--version") if python else (),
                blocker="Python is unavailable." if python is None else "",
            ),
        )
        commands = (
            VerificationCommand(
                id="pytest",
                argv=(str(python), "-m", "pytest", "-q"),
                timeout_seconds=1200,
            ),
        ) if python else ()
        blockers = tuple(probe.blocker for probe in probes if probe.blocker)
        return ToolchainProfile(
            id="python-saas",
            label="Python SaaS",
            probes=probes,
            verification_commands=commands,
            blockers=blockers,
            docker_image="python:3.12-local",
        )

    def _node_profile(self, root: Path) -> ToolchainProfile:
        node = self._tool("node", ("node",))
        npm = self._tool("npm", ("npm", "npm.cmd"))
        probes = (
            ToolchainProbe(
                "node",
                node is not None,
                node,
                (str(node), "--version") if node else (),
                "Node.js is unavailable." if node is None else "",
            ),
            ToolchainProbe(
                "npm",
                npm is not None,
                npm,
                (str(npm), "--version") if npm else (),
                "npm is unavailable." if npm is None else "",
            ),
        )
        manifest = _read_json(root / "package.json")
        scripts = manifest.get("scripts") if isinstance(manifest, dict) else {}
        scripts = scripts if isinstance(scripts, dict) else {}
        commands: list[VerificationCommand] = []
        if npm is not None and str(scripts.get("test") or "").strip():
            commands.append(VerificationCommand("node-test", (str(npm), "test"), timeout_seconds=1200))
        if npm is not None and str(scripts.get("typecheck") or "").strip():
            commands.append(
                VerificationCommand("node-typecheck", (str(npm), "run", "typecheck"), timeout_seconds=1200)
            )
        if npm is not None and str(scripts.get("test:e2e") or "").strip():
            commands.append(
                VerificationCommand(
                    "playwright-e2e",
                    (str(npm), "run", "test:e2e"),
                    timeout_seconds=1800,
                    artifact_kind="playwright",
                )
            )
        blockers = [probe.blocker for probe in probes if probe.blocker]
        if not commands and not blockers:
            blockers.append("package.json has no approved test, typecheck, or test:e2e script.")
        return ToolchainProfile(
            id="node-saas",
            label="Node SaaS",
            probes=probes,
            verification_commands=tuple(commands),
            blockers=tuple(blockers),
            docker_image="node:22-local",
        )

    def _flutter_profile(self, root: Path) -> ToolchainProfile:
        flutter_home = self.env.get("FLUTTER_HOME", "")
        android_home = self.env.get("ANDROID_HOME") or self.env.get("ANDROID_SDK_ROOT") or ""
        local_app_data = self.env.get("LOCALAPPDATA", "")
        flutter_candidates = [
            str(Path(flutter_home) / "bin" / "flutter.bat") if flutter_home else "",
            "C:/tools/flutter/bin/flutter.bat" if self.include_system_paths else "",
            "flutter",
        ]
        flutter = self._tool("flutter", tuple(item for item in flutter_candidates if item))
        dart_candidates = [
            str(Path(flutter_home) / "bin" / "dart.bat") if flutter_home else "",
            str(Path(flutter).parent / "dart.bat") if flutter else "",
            "C:/tools/flutter/bin/dart.bat" if self.include_system_paths else "",
            "dart",
        ]
        dart = self._tool("dart", tuple(item for item in dart_candidates if item))
        sdk_candidates = [Path(android_home)] if android_home else []
        if self.include_system_paths and local_app_data:
            sdk_candidates.append(Path(local_app_data) / "Android" / "Sdk")
        adb = self._first_file([sdk / "platform-tools" / "adb.exe" for sdk in sdk_candidates])
        emulator = self._first_file([sdk / "emulator" / "emulator.exe" for sdk in sdk_candidates])
        if adb is None:
            adb = self._tool("adb", ("adb",))
        if emulator is None:
            emulator = self._tool("emulator", ("emulator",))
        gradle = self._first_file((root / "android" / "gradlew.bat", root / "android" / "gradlew"))
        package_config = root / ".dart_tool" / "package_config.json"
        dependencies_ready = package_config.is_file()

        probes = (
            _probe("flutter", flutter, ("--version",), "Flutter is unavailable. Install it at C:\\tools\\flutter or set FLUTTER_HOME."),
            _probe("dart", dart, ("--version",), "Dart is unavailable through the Flutter SDK."),
            _probe("gradle-wrapper", gradle, ("--version",), "The workspace Android Gradle wrapper is unavailable."),
            _probe("adb", adb, ("version",), "ADB is unavailable. Set ANDROID_HOME to the Android SDK."),
            _probe("android-emulator", emulator, ("-list-avds",), "The Android emulator CLI is unavailable."),
            ToolchainProbe(
                "flutter-dependencies",
                dependencies_ready,
                package_config.resolve() if dependencies_ready else None,
                (),
                (
                    "Flutter dependencies are not resolved. Run 'flutter pub get' manually "
                    "before governed verification."
                    if not dependencies_ready
                    else ""
                ),
            ),
        )
        commands: list[VerificationCommand] = []
        if flutter is not None and dependencies_ready:
            commands.extend(
                [
                    VerificationCommand(
                        "flutter-analyze",
                        (str(flutter), "analyze", "--no-pub"),
                        timeout_seconds=1200,
                    ),
                    VerificationCommand(
                        "flutter-test",
                        (str(flutter), "test", "--no-pub"),
                        timeout_seconds=1800,
                    ),
                    VerificationCommand(
                        "flutter-apk-debug",
                        (str(flutter), "build", "apk", "--debug", "--no-pub"),
                        timeout_seconds=3600,
                        artifact_kind="apk",
                    ),
                ]
            )
        required_ids = {
            "flutter",
            "dart",
            "adb",
            "flutter-dependencies",
        }
        blockers = tuple(
            probe.blocker for probe in probes if probe.id in required_ids and probe.blocker
        )
        return ToolchainProfile(
            id="flutter-mobile",
            label="Flutter Mobile",
            probes=probes,
            verification_commands=tuple(commands),
            blockers=blockers,
        )

    @staticmethod
    def _generic_profile() -> ToolchainProfile:
        return ToolchainProfile(
            id="generic",
            label="Generic Repository",
            probes=(),
            verification_commands=(),
            blockers=("No supported SaaS or mobile toolchain was detected.",),
        )

    def _tool(self, name: str, candidates: tuple[str, ...]) -> Path | None:
        for raw in candidates:
            if not raw:
                continue
            candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
            if candidate.is_file():
                return candidate.resolve()
            if candidate.is_absolute():
                continue
            located = self.which(raw if raw not in {"npm.cmd"} else name)
            if located and Path(located).is_file():
                return Path(located).resolve()
        return None

    @staticmethod
    def _first_file(candidates) -> Path | None:
        for candidate in candidates:
            path = Path(candidate)
            if path.is_file():
                return path.resolve()
        return None


def _probe(tool_id: str, path: Path | None, args: tuple[str, ...], blocker: str) -> ToolchainProbe:
    return ToolchainProbe(
        id=tool_id,
        available=path is not None,
        path=path,
        version_argv=(str(path), *args) if path else (),
        blocker="" if path else blocker,
    )


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
