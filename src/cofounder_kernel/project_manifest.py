from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectManifest:
    name: str
    product_type: str
    lifecycle_state: str
    distribution_targets: tuple[str, ...]
    scaffold_on_intake: bool
    body: str = ""


def load_project_manifest(path: str | Path) -> ProjectManifest:
    manifest_path = Path(path)
    text = manifest_path.read_text(encoding="utf-8-sig")
    if not text.startswith("---"):
        raise ValueError(f"Project manifest must begin with YAML front matter: {manifest_path}")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Project manifest front matter is not closed: {manifest_path}")
    values: dict[str, object] = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value = raw_value.strip().strip("\"'")
        if value.startswith("[") and value.endswith("]"):
            values[key.strip()] = tuple(
                item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()
            )
        elif value.lower() in {"true", "false"}:
            values[key.strip()] = value.lower() == "true"
        else:
            values[key.strip()] = value
    name = str(values.get("name") or "").strip()
    product_type = str(values.get("product_type") or "").strip()
    targets = tuple(values.get("distribution_targets") or ())
    if not name or not product_type:
        raise ValueError(f"Project manifest requires name and product_type: {manifest_path}")
    if not targets:
        raise ValueError(f"Project manifest requires distribution_targets: {manifest_path}")
    return ProjectManifest(
        name=name,
        product_type=product_type,
        lifecycle_state=str(values.get("lifecycle_state") or "intake").strip(),
        distribution_targets=tuple(str(item) for item in targets),
        scaffold_on_intake=bool(values.get("scaffold_on_intake", False)),
        body=parts[2].strip(),
    )


def write_project_manifest(path: str | Path, manifest: ProjectManifest) -> Path:
    manifest_path = Path(path)
    targets = ", ".join(manifest.distribution_targets)
    text = (
        "---\n"
        f"name: {manifest.name}\n"
        f"product_type: {manifest.product_type}\n"
        f"lifecycle_state: {manifest.lifecycle_state}\n"
        f"distribution_targets: [{targets}]\n"
        f"scaffold_on_intake: {'true' if manifest.scaffold_on_intake else 'false'}\n"
        "---\n\n"
        f"{manifest.body.strip()}\n"
    )
    manifest_path.write_text(text, encoding="utf-8")
    return manifest_path
