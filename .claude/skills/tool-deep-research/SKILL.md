---
name: tool-deep-research
description: "Use when the user asks about the deep research tool profile, research_kickoff_tool, clarify_with_text, start_research_task, citation-heavy online research, or differences between OpenAI deep research and Zade's local research workflow."
---

# Tool Deep Research

Use this profile when the user asks about OpenAI-style deep research tooling, `research_kickoff_tool`, `clarify_with_text`, `start_research_task`, or citation-heavy online research behavior.

## Tool profile boundary

This skill makes the attached deep-research tool profile visible to Zade's skill router. It does not grant `research_kickoff_tool` to the local kernel. Before claiming a research task has started, check live inventory and the approved research/action-handler state.

## Source

Imported source: `tool-deep-research.md`, preserved at `references/source.md`.

Read the source when exact deep-research prompt behavior or citation guidance matters.

## Local Mapping

- For Zade's local workflow skill, prefer `deep-research`.
- For real web fetches in this kernel, use the approval-gated `external.research.run` path.
- Ask clarifying questions before research when the topic is underspecified.
