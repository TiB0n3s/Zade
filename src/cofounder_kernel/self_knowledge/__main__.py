from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .drift import DEFAULT_ALLOWLIST_PATH, DriftFinding, check_self_knowledge_file
from .renderer import render_self_knowledge
from .snapshot import DEFAULT_DOC_PATH, collect_snapshots, refresh_doc


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render, refresh, or check Zade's living self-knowledge document.")
    parser.add_argument("--doc", default=str(DEFAULT_DOC_PATH), help="Path to the self-knowledge markdown file.")
    parser.add_argument("--repo-root", default=".", help="Repository root used for snapshots and drift checks.")
    parser.add_argument(
        "--allowlist",
        default=str(DEFAULT_ALLOWLIST_PATH),
        help="Path to newline-delimited drift-check allowlist.",
    )
    parser.add_argument("--render", action="store_true", help="Print the rendered document without writing it.")
    parser.add_argument("--refresh", action="store_true", help="Write generated AUTO block updates to --doc.")
    parser.add_argument("--check", action="store_true", help="Check hand-written references for stale pointers.")
    parser.add_argument("--strict", action="store_true", help="Return nonzero when --check finds drift.")
    args = parser.parse_args(argv)

    doc_path = Path(args.doc)
    repo_root = Path(args.repo_root)
    allowlist_path = Path(args.allowlist)

    if args.render:
        text = doc_path.read_text(encoding="utf-8")
        snapshots = collect_snapshots(repo_root=repo_root, doc_path=doc_path)
        print(render_self_knowledge(text, snapshots).rstrip())
        return 0

    if args.check or args.strict:
        findings = check_self_knowledge_file(
            doc_path,
            repo_root=repo_root,
            allowlist_path=allowlist_path,
            snapshots=collect_snapshots(repo_root=repo_root, doc_path=doc_path),
        )
        _print_findings(findings)
        return 1 if findings and args.strict else 0

    result = refresh_doc(doc_path=doc_path, repo_root=repo_root)
    status = "updated" if result["changed"] else "unchanged"
    print(f"{status}: {result['path']}")
    return 0


def main() -> None:
    raise SystemExit(run())


def _print_findings(findings: list[DriftFinding]) -> None:
    if not findings:
        print("no drift findings")
        return
    print("kind\treference\tlocation\treason")
    for finding in findings:
        print(
            f"{finding.kind}\t{finding.reference}\t"
            f"{finding.location_in_doc}\t{finding.reason}"
        )


if __name__ == "__main__":
    main()
