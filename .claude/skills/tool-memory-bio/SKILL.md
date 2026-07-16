---
name: tool-memory-bio
description: "Use when the user asks about the bio memory tool, persistent user facts, remembering preferences across conversations, or mapping bio-style memory to Zade's local memory system. Never persist secrets or transient task state."
---

# Tool Memory Bio

Use this profile when the user asks about a bio-style persistent memory tool, cross-conversation user preferences, or "remember this" behavior.

## Tool profile boundary

This skill makes the attached bio memory profile visible to Zade's skill router. It does not grant access to ChatGPT's `bio` tool. In Zade, persistence must go through the local memory tool and authority boundaries.

## Source

Imported source: `tool-memory-bio.md`, preserved at `references/source.md`.

Read the source when exact bio-tool wording matters.

## Local Mapping

- Use `memory.write` only for durable facts the founder explicitly asks Zade to remember.
- Do not store secrets, credentials, client/network specifics, or transient task state.
- Use `memory.forget` when the founder asks to remove a stored local memory.
