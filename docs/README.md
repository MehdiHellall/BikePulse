# BikePulse documentation

Start with the document that matches the work you are doing.

## Current system

- [Architecture](architecture.md): package boundaries, runtime data flow, and repository tree.
- [Data contract](data-contract.md): timestamps, observation schema, freshness, labels, and artifacts.
- [Operations](operations.md): collector lifecycle, safe checks during an experiment, recovery, and ML commands.

## Plans

- [Live map POC](plans/live-map-poc.md): delivered map scope and acceptance criteria.
- [Offline ML POC](plans/ml-poc.md): current collection and empty-bike evaluation contract.
- [Product MVP](plans/mvp.md): longer roadmap for live forecasts, full-station risk, and replay.

Plans describe different delivery scopes. The root README and current-system
documents describe behavior that is implemented now.

## Audits

- [13 October 2026 collection and label audit](audits/2026-10-13-collection.md)

Audit files are immutable point-in-time records. Later operational results belong
in a new dated audit rather than rewriting an earlier record.
