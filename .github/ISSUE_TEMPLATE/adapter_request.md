---
name: Adapter request
about: Propose or request a source adapter for a new stack (app, runtime, or store)
title: "adapter: <stack name>"
labels: ["adapter"]
---

## The stack
<!-- Which tool/app/runtime? e.g. LibreChat, LM Studio, a specific vector DB. Version(s)? -->

## What state does it hold, and where?
<!-- A DB file? a directory? an API? Which parts are conversation/memory vs. config/cache/weights? -->

## Portability
<!-- Machine-local or synced? Does the on-disk/API format change between releases? -->

## Willing to implement it?
- [ ] I can write the adapter against the `SourceAdapter` contract (see `docs/ADAPTERS.md`) and run the conformance kit
- [ ] I'm requesting that someone else build it
