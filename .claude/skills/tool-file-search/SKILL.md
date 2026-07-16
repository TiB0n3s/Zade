---
name: tool-file-search
description: "Use when the user asks about file_search, msearch, uploaded file retrieval, document search, citations from uploaded files, QDF freshness, or how to query user-provided/internal documents. Map to Zade's local ingestion and semantic search where applicable."
---

# Tool File Search

Use this profile for file search, uploaded document retrieval, internal knowledge search, `msearch`, citations from file lines, or QDF-style query freshness.

## Tool profile boundary

This skill makes the attached file_search tool contract visible to Zade's skill router. It does not create a live `file_search` namespace unless the runtime inventory lists one. Use Zade's local memory, ingestion, and semantic-search surfaces when those are the actual available tools.

## Source

Imported source: `tool-file_search.md`, preserved at `references/source.md`.

Read the source when exact query construction, QDF, multilingual querying, or citation formatting matters.

## Local Mapping

- For local memories, use `memory.search`.
- For ingested documents, use the semantic search/context routes available in the live kernel.
- Cite exact local files or evidence when possible; do not invent file_search citation IDs.
