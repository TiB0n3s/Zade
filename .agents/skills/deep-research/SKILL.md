---
name: deep-research
description: Deep research harness for deep, multi-source, fact-checked research reports on any topic. Use when the user asks for deep research, a multi-source cited report, fact checking, adversarial claim verification, source synthesis, or an investigation requiring web search and source fetching. Before invoking, check whether the question is specific enough to research directly; if underspecified, ask 2-3 clarifying questions to narrow scope, then pass the refined question as args.
---

# Deep Research

Run the `deep-research` workflow when the user needs a cited research report rather than a quick answer.

## Scope Gate

Before invoking:
- Confirm the question is specific enough to research directly.
- If it is underspecified, ask 2-3 clarifying questions about the missing constraints, such as budget, use case, region, timeframe, audience, or decision criteria.
- Weave the answers into one refined research question and pass that as the workflow args.

## Invocation

Invoke: `Workflow({ name: "deep-research" })`

When supported, pass the refined question explicitly:

```js
Workflow({ name: "deep-research", args: "<refined research question>" })
```

The executable workflow source is bundled at `scripts/workflow-script.js`.

## Workflow

Use this phase structure:

- Scope: Decompose the refined question into 5 search angles.
- Search: Run 5 parallel WebSearch agents, one per angle.
- Fetch: Deduplicate URLs, fetch the top 15 sources, and extract falsifiable claims.
- Verify: Run 3-vote adversarial verification per claim; require 2 of 3 refutations to kill a claim.
- Synthesize: Merge semantic duplicates, rank by confidence, and cite sources.

Return a cited report with findings, confidence levels, caveats, open questions, source list, and verification stats.
