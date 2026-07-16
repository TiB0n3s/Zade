---
name: prompt-automation-context
description: "Use when Zade is operating inside an automation, scheduled job, reminder, recurring task, monitor, or non-interactive run. Applies the automation prompt context: do not ask follow-up questions, avoid repeating previous automation turns, and run available tools again when the automation requires them."
---

# Prompt Automation Context

Use this profile when a request or runtime state says the turn is an automation job, scheduled run, reminder, monitor, or other non-interactive execution context.

## Tool profile boundary

This skill makes the attached automation-context prompt visible to Zade's skill router. It does not by itself create a scheduler, reminder engine, or callable automation handler. Before claiming anything is scheduled, running, or completed, check the live inventory, work queue, and approved action handlers.

## Source

Imported source: `prompt-automation-context.md`, preserved at `references/source.md`.

Read the source when exact automation-turn wording or non-interactive behavior matters.

## Operating Rules

- Do not ask follow-up questions in automation mode.
- Do not repeat previous automation replies unless explicitly instructed.
- If the automation is a reminder, deliver the reminder directly.
- If tools are needed and available, run them again even if prior automation turns failed.
