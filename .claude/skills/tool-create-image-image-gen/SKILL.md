---
name: tool-create-image-image-gen
description: "Use when the user asks about image_gen, DALL-E style image creation, image editing, transforming an attached image, generating visual assets, or image-generation tool syntax and safety boundaries. Treat as an image_gen tool profile unless a live image tool exists."
---

# Tool Create Image Image Gen

Use this profile for image generation and image editing requests, especially when the user names `image_gen`, asks for visual assets, or wants an attached image changed.

## Tool profile boundary

This skill makes the attached image_gen tool contract visible to Zade's skill router. It does not by itself grant this local runtime an image-generation handler. Before claiming an image was generated or edited, check the live tool inventory.

## Source

Imported source: `tool-create-image-image_gen.md`, preserved at `references/source.md`.

Read the source when exact image_gen syntax or post-generation behavior matters.

## Operating Rules

- If a live image tool exists, follow its generation/editing contract exactly.
- If no live image tool exists, say the capability is not available in this runtime.
- Apply `prompt-image-safety-policies` for image requests involving people or identity.
