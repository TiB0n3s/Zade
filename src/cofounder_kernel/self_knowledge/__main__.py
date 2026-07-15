from __future__ import annotations

import argparse
from pathlib import Path

from .snapshot import DEFAULT_DOC_PATH, refresh_doc


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh Zade's living self-knowledge document.")
    parser.add_argument("--doc", default=str(DEFAULT_DOC_PATH), help="Path to the self-knowledge markdown file.")
    args = parser.parse_args()

    result = refresh_doc(doc_path=Path(args.doc))
    status = "updated" if result["changed"] else "unchanged"
    print(f"{status}: {result['path']}")


if __name__ == "__main__":
    main()
