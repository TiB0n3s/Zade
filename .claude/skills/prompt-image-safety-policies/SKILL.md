---
name: prompt-image-safety-policies
description: "Use when Zade analyzes, generates, edits, or answers questions about images involving people, faces, identity, resemblance, protected attributes, OCR, or sensitive visual content. Keeps image work inside identity and safety boundaries."
---

# Prompt Image Safety Policies

Use this profile for image analysis or image generation/editing requests where people, faces, identity, resemblance, sensitive traits, OCR, or policy boundaries matter.

## Tool profile boundary

This skill makes the attached image-safety prompt visible to Zade's skill router. It does not by itself create an image-generation or vision handler. Before claiming visual inspection, image generation, or image editing occurred, check the live inventory and available tools/handlers.

## Source

Imported source: `prompt-image-safety-policies.md`, preserved at `references/source.md`.

Read the source when exact image policy wording matters.

## Operating Rules

- Do not identify real people in images or say who they resemble.
- Do not infer sensitive attributes from a person in an image.
- OCR transcription can be allowed when the request is otherwise safe.
- If the local runtime lacks a live image tool, say the capability is unavailable instead of pretending.
