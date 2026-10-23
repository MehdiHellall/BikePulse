# Paris Atlas interface direction

## Design baseline

BikePulse is a live urban-mobility tool. Riders come to the first screen to find a
station, read current bike or dock availability and decide whether that supply is
likely to hold for the next 30 minutes.

The selected direction treats the map as a contemporary Paris street atlas. Warm
paper, ink, cobalt and restrained status colors establish the visual language. The
map remains the first-screen anchor; station identity, inventory and outlook form one
horizontal strip immediately below it rather than separate dashboard cards.

The interface targets desktop first while retaining Streamlit's responsive column
stacking. It preserves explicit live, last-known, stale, unavailable and weather-fallback
states. Outlooks remain rule-based estimates and are never presented as
probabilities or validated predictions.

## Signature refinement

The refined north star is a contemporary Paris street atlas with the immediacy of a
cycling control room. The interface should feel like a maintained public-facing
service, not a concept dashboard: exact feed times, automatic-refresh cadence,
inventory counts, stale states and the rule disclaimer carry more visual weight than
decoration.

The signature comes from the oversized editorial wordmark, a plotted coordinate grid,
ink map rules, cobalt registration marks and a route-like decision rail from live feed
to station inventory to the 30-minute outlook. The rail is product navigation rather
than ornament. The upper-case service eyebrow is intentionally retained as atlas
wayfinding; the screenshot linter flags it as a generic eyebrow pattern, but here it
identifies the service, geography and data mode before the wordmark.

The previous Streamlit chrome and generic success card were removed. Network health,
feed time and station count now form one responsive operational bar. On phones that
bar compresses to a single row, the search remains full width, mode and refresh actions
share the next row, and a useful part of the map is visible in the initial 390 by 844
viewport. Supporting labels were enlarged rather than hidden.

## Interaction and motion

- The first visit reveals the title from a clipping mask while the three-stop decision
  route draws left to right. The total entrance remains under one second.
- Buttons use a short two-pixel press response and a hard cobalt focus/hover shadow,
  like a map-case latch closing.
- Selecting a station redraws the map around the chosen point and unfolds the
  station strip with a cobalt rule tracing across its top edge.
- Availability mode, reason disclosure and refresh feedback remain close to the
  initiating control.
- Reduced-motion preferences remove every custom animation and transition without
  hiding or reordering content.

## Acceptance focus

- The map, current inventory and outlook read as one decision sequence.
- Fresh, delayed, stale and feed-level freshness states remain visible.
- Bikes and docks modes retain identical turnover assumptions.
- Missing or stale information remains unknown and visibly neutral.
- The live map remains usable through PyDeck selection and station search.

Baseline screenshots remain in `docs/assets/`. Canonical signature evidence is in
`evidence-signature-final-v2/`, including the first screen, 2x capture, motion probe and
entry/selection frames. The fresh selected-station state is in
`evidence-signature-selected-v2/`; the final phone viewport is in
`evidence-signature-mobile-final4/`. Browser checks found no console errors,
horizontal overflow, failed images or readability lint. FFmpeg was unavailable, so
motion is represented by start, middle and end frames rather than an MP4.

## Review record

An independent visual review found no blocker and asked for one high-impact correction:
bring the map into the first phone viewport while increasing the smallest supporting
labels. It confirmed that the paper grid, ink rules, cobalt registration details,
explicit freshness and validation disclaimer make the result distinctive without
feeling speculative. The final mobile pass incorporates that feedback and keeps all
operational safeguards visible.

The independent UX reviewer could not access a browser surface and did not fabricate a
walkthrough. The real Playwright journey therefore supplied interaction evidence. It
completed station selection, inventory/outlook reading, reason disclosure, dock-mode
switching and feed refresh three consecutive times without a failure. Sound, touch and
physical-device behavior remain outside this pass.
