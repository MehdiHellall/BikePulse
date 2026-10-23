# BikePulse documentation

Start with the document that matches the work you are doing.

## Current system

- [Architecture](architecture.md): package boundaries, runtime data flow, and repository tree.
- [Data contract](data-contract.md): timestamps, observation schema, freshness, labels, and artifacts.
- [Operations](operations.md): collector lifecycle, safe checks during an experiment, recovery, and ML commands.

## Plans

- [Live map POC](plans/live-map-poc.md): delivered map scope and acceptance criteria.
- [Offline ML POC](plans/ml-poc.md): current collection and empty-bike evaluation contract.
- [Frozen dataset export](plans/frozen-dataset.md): use the completed partial collection for
  representative and training-enriched datasets.
- [Guided frozen model study](plans/frozen-model-study.md): completed offline comparisons,
  recorded selection/test results, review checkpoints, and a later deployment handoff.
- [Product MVP](plans/mvp.md): longer roadmap for live forecasts, full-station risk, and replay.

Plans describe different delivery scopes. The root README and current-system
documents describe behavior that is implemented now.

## Research

- [Past-only temporal feature study](research/temporal-feature-study.md): rolling development
  validation, feature ablations, horizon experiments, uncertainty, and the next research gate.
- [Temporal feature catalog](research/temporal-feature-catalog.md): exact formulas, horizons,
  missing-state behavior, and causal availability for every added feature.
- Reproduction configuration: [`experiments/temporal-history.toml`](../experiments/temporal-history.toml).

## Audits

- [23 October 2026 audit remediation](audits/2026-10-23-remediation.md): dependency,
  frozen-lifecycle, malformed-record, temporal-provenance, and integration-test fixes.
- [23 October 2026 project audit and session baseline](audits/2026-10-23-project.md):
  verification results, dependency findings, reproducible defects, and cleanup scope.
- [13 October 2026 collection and label audit](audits/2026-10-13-collection.md)

Audit files are immutable point-in-time records. Later operational results belong
in a new dated audit rather than rewriting an earlier record.
