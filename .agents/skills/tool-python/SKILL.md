---
name: tool-python
description: "Use when the user asks about Python notebook/code-interpreter style execution, data analysis in Python, pandas dataframes, charting rules, or Python tool constraints. Distinguish this profile from Zade's approved local dev command runner."
---

# Tool Python

Use this profile when the user asks for Python notebook/code-interpreter behavior, data analysis, dataframe display, or charting constraints.

## Tool profile boundary

This skill makes the attached Python tool contract visible to Zade's skill router. It does not grant an unrestricted Jupyter notebook to the local kernel. Before claiming Python code has run, check live inventory and approved dev/action handlers.

## Source

Imported source: `tool-python.md`, preserved at `references/source.md`.

Read the source when exact Python notebook or charting rules matter.

## Local Mapping

- Zade's local dev lane is `dev.command.run`, limited to allowlisted commands and approved dispatch.
- Do not claim internet access or notebook persistence unless the active runtime provides it.
- For repo work, prefer the project's configured interpreter and tests.
