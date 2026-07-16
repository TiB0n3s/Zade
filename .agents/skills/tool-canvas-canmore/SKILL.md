---
name: tool-canvas-canmore
description: "Use when the user asks about canvas, canmore, textdocs, creating or updating a long document, editing code in a side canvas, or commenting on an existing textdoc. Treat this as a canmore/canvas tool profile unless a live canvas handler exists."
---

# Tool Canvas Canmore

Use this profile when the request involves a canvas, canmore, textdoc, long document/code iteration, or comments on an existing canvas document.

## Tool profile boundary

This skill makes the attached canmore/canvas tool contract visible to Zade's skill router. It does not create a live canmore canvas in this local kernel. Before claiming a canvas was created or updated, check whether a live canvas/textdoc tool exists in inventory.

## Source

Imported source: `tool-canvas-canmore.md`, preserved at `references/source.md`.

Read the source when exact create/update/comment semantics matter.

## Local Mapping

- If no canvas tool exists, use local file/report/draft handlers only through the approved paths.
- For code or document edits in this repo, write files directly only when the current environment and authority allow it.
- Do not claim a side canvas exists unless the live tool is present.
