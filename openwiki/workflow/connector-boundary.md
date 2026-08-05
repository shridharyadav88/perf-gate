---
type: scope boundary
title: OpenWiki connector extension boundary
description: Records the connector contract relevant to repository operations while distinguishing it from this Python package, which does not implement connectors.
tags: [connectors, operations, security, scope]
---

# OpenWiki connector extension boundary

This repository is a Python package for deploying the `code-optimizer` skill; it contains no `src/connectors` implementation, connector registry, `ConnectorRuntime`, or `openwiki personal --update` command. The following contract is an external OpenWiki extension boundary, recorded so agents do not mistake it for an extension seam in this package. Its source is `/skills/write-connector/SKILL.md`.

## External connector contract

In the OpenWiki OSS repository, a built-in connector is added to `src/connectors/types.ts` and `src/connectors/registry.ts`, with implementation under `src/connectors/sources/<connector>.ts`. It exposes a `ConnectorRuntime` with `id`, `displayName`, `description`, `backend`, `requiredEnv`, `supportsAgenticDiscovery`, and `ingest()`.

Ingestion writes raw JSON and manifests under `~/.openwiki/connectors/<id>/raw/<run-id>/`; persistent cursor/state data belongs in `state.json`, connector settings in `config.json`, and secrets in `~/.openwiki/.env` referenced only by environment-variable name. Connector IDs and raw paths must be validated to stay within the connector directory.

## Security and provenance

Connector code must not read, print, log, return, hardcode, or persist secret values. Credentialed fetches must be deterministic. MCP wrappers are read-only and limited to allowlisted read/dump operations; untrusted manifests cannot choose arbitrary commands or network endpoints. Timestamped sources retain per-stream cursors; object sources retain IDs, edit timestamps, and content hashes; paginated sources retain continuation state. Raw records preserve IDs, timestamps, URLs, authors, and citation provenance.

A connector implementation's user-facing finish must name changed files, required `.env` variables, config edits, provider scopes/permissions, and the `openwiki personal --update` command. None of these operations are performed by this repository's `install-agent-skills` CLI.
