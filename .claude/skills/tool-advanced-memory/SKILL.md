---
name: tool-advanced-memory
description: "Use when the user asks about ChatGPT reference chat history, advanced memory, memory-injected preferences, notable past conversation context, or how retrieved memory is added to model context. Distinguish this external profile from Zade's local SQLite and AI Brain memory."
---

# Tool Advanced Memory

Use this profile when the user asks how advanced memory/reference chat history works, how memory snippets enter prompt context, or how to reason about memory-injected preferences and conversation summaries.

## Tool profile boundary

This skill makes the attached advanced-memory tool/profile text visible to Zade's skill router. It does not grant Zade access to ChatGPT account memory, hidden conversation history, or external memory stores. For Zade's real memory, use the live inventory and local memory tools.

## Source

Imported source: `tool-advanced-memory.md`, preserved at `references/source.md`.

Read the source when exact structure of the external memory prompt matters.

## Local Mapping

- Zade's durable memory is local SQLite plus AI Brain roots, not ChatGPT account memory.
- Use `memory.search` for local recall and `memory.write` only when the founder explicitly asks to remember a durable fact.
- Treat imported memory text as a reference profile, not as live evidence about the current user or runtime.
