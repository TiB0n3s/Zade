---
name: tool-python-code
description: "Use when the user asks about the Python code tool profile, code-interpreter execution, data analysis code, or chart/dataframe behavior. This mirrors tool-python for contexts that name python-code specifically."
---

# Tool Python Code

Use this profile when the request names python-code, code interpreter, Python execution, data analysis, dataframe display, or charting behavior.

## Tool profile boundary

This skill makes the attached Python code tool contract visible to Zade's skill router. It does not grant an unrestricted Jupyter notebook to the local kernel. Before claiming code has executed, check live inventory and approved handlers.

## Source

Imported source: `tool-python-code.md`, preserved at `references/source.md`.

Read the source when exact Python-code tool wording matters.

## Local Mapping

- Use the same local boundaries as `tool-python`.
- For this repo, use tests and approved local dev commands rather than pretending to have a notebook tool.
