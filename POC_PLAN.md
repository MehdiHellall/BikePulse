# BikePulse Station Map POC

## Goal

The current proof of concept is a local Streamlit application for exploring current Vélib’ station availability. It displays the official station network, supports bike and dock availability modes, lets users search or click a station, and explains stale or unavailable data.

Machine learning, forecasts, replay, historical charts, deployment, and route planning remain in `MVP_PLAN.md`.

## Delivery slices

1. **Baseline:** reproducible Python 3.12 environment, lockfile, active CI checks, and documented commands.
2. **Live data:** cached station information and status feeds joined by `station_id`, with validation and last-known-data recovery.
3. **Map:** a PyDeck station layer with observed availability colors, timestamps, legend, and data-quality counts.
4. **Interaction:** name/ID search, map selection, bike/dock toggle, and a detailed station panel.
5. **Validation:** unit tests for data and classification rules, Streamlit behavior tests, browser smoke tests, packaging, and a manual live-data check.

## Acceptance

- Every valid geocoded station from the information feed is represented once.
- Missing values remain unknown and never become zero.
- Stale, offline, and last-known data use grey markers and explicit labels.
- Search and map clicks resolve stations by stable `station_id` values.
- Automatic and manual refresh preserve mode and selection.
- Tests never call the public GBFS feed.
- A clean checkout can install, run checks, and start the app from the README.

## Validation record

On 5 October 2026, a live check loaded 1,519 stations with no rejected, excluded,
or missing-status records. Headless Chromium rendered the cached app in 1.318 seconds;
the two external feed requests took 0.401 seconds in the same environment. These are
local reference measurements rather than service guarantees.
