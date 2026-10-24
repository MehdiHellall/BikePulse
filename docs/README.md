# BikePulse documentation

- [Architecture](architecture.md): package boundaries and live request flow.
- [Data contract](data-contract.md): feed validation, timestamps, freshness and unknown states.
- [Prediction rules](prediction-rules.md): rule version 5, bounded recent movement, nearby
  pressure, flexible freshness, weather and geographic/time assumptions.
- [Operations](operations.md): setup, configuration, refresh, verification and troubleshooting.

The app has one live availability page. Station details contain the 30-minute outlook.

## UI examples

These screenshots use controlled synthetic station and weather fixtures.

- [Bike outlook with explanations](assets/live-bike-outlook.png)
- [Dock outlook](assets/live-dock-outlook.png)
