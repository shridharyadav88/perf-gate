---
type: operations workflow
title: OpenWiki update operations
description: Documents the scheduled and manual GitHub Actions workflow that regenerates this wiki and proposes changes through a pull request.
tags: [operations, github-actions, openwiki, automation]
---

# OpenWiki update operations

`.github/workflows/openwiki-update.yml` runs the repository documentation refresh either manually (`workflow_dispatch`) or daily at `0 8 * * *`. It grants `contents: write` and `pull-requests: write` because the final step creates a branch and pull request containing generated changes.

## Runtime contract

The workflow runs on `ubuntu-latest`, checks out the repository with pinned `actions/checkout` and `fetch-depth: 0`, and installs Node.js 22 with pinned `actions/setup-node`. Full history is required because `openwiki code --update` compares against the commit it last documented. It globally installs `openwiki@0.3.1`, `mermaid@11.16.0`, and `jsdom@29.1.1`; the latter two enable diagram validation.

The OpenWiki step uses the `openrouter` provider, model `z-ai/glm-5.2`, and repository secrets `OPENROUTER_API_KEY` and `OPENWIKI_LANGSMITH_API_KEY`. Optional LangSmith tracing uses `LANGSMITH_API_KEY`, `LANGCHAIN_PROJECT=openwiki`, and `LANGCHAIN_TRACING_V2=true`. Secrets are configuration inputs only and must never be copied into wiki content.

```mermaid
sequenceDiagram
    participant Scheduler as GitHub scheduler
    participant Actions as Actions runner
    participant Generator as OpenWiki CLI
    participant Repo as Repository branch
    participant PR as Pull request
    Scheduler->>Actions: Dispatch scheduled or manual run
    Actions->>Actions: Checkout full history and install Node tooling
    Actions->>Generator: openwiki code --update --print with provider secrets
    Generator->>Repo: Generate openwiki and allowed config updates
    Actions->>PR: Create openwiki/update pull request
```
Caption: The workflow regenerates documentation on a full-history runner and proposes the result instead of pushing directly to the default branch.

## Generated paths and change surface

The pull-request action adds `openwiki`, `AGENTS.md`, `CLAUDE.md`, and `.github/workflows/openwiki-update.yml` on branch `openwiki/update`, with commit/title `docs: update OpenWiki`. A failed checkout, missing provider secret, model/provider error, or Mermaid validation issue prevents a trustworthy update. Changes to workflow permissions, pinned action versions, generated paths, provider/model variables, or secret names require operational review.
