---
name: tool-web-search
description: "Use when the user asks about web search, browsing, current information, local information, freshness-sensitive facts, or the OpenAI web tool profile. Map to Zade's approval-gated web research lane when no direct web tool exists."
---

# Tool Web Search

Use this profile for web search, browsing, current facts, local information, freshness-sensitive answers, or requests that name the `web` tool.

## Tool profile boundary

This skill makes the attached web-search tool contract visible to Zade's skill router. It does not grant a direct `web` namespace to the local kernel. Before claiming a search was performed, check live inventory and use the real approved research path.

## Source

Imported source: `tool-web-search.md`, preserved at `references/source.md`.

Read the source when exact web-search trigger rules matter.

## Local Mapping

- For Zade's actual web lane, use `external.research.run` after approval.
- For a deep cited report, prefer the `deep-research` skill and approval-gated research service.
- Do not use or mention deprecated browser tooling as a web-search substitute.
