# Paris Atlas interface direction

## Design baseline

BikePulse is a live urban-mobility tool. Riders come to the first screen to find a
station, read current bike or dock availability and decide whether that supply is
likely to hold for the next 30 minutes.

The selected direction treats the map as a contemporary Paris street atlas. Warm
paper, ink, cobalt and restrained status colors establish the visual language. Before
selection the map is the first-screen anchor. After selection, station identity,
inventory and outlook move directly beneath the controls, with the map continuing the
decision spatially below rather than delaying the answer.

The interface targets desktop first while retaining Streamlit's responsive column
stacking. It preserves explicit live, last-known, stale, unavailable and weather-fallback
states. Outlooks remain rule-based estimates and are never presented as
probabilities or validated predictions.

## Signature refinement

The refined north star is a contemporary Paris street atlas with the calm of a
maintained public service. The editorial wordmark is smaller, the coordinate eyebrow
and ornamental step rail are gone, and the page-wide grid is limited to a faint header
texture. Thin ink rules replace heavy cards and shadows.

Network health, feed time and station count form one compact operational header. The
plain task line “Find a Vélib.” is grouped with its search field; mode and refresh form
a separate action pair. On phones the task and search use the full row, with mode and
refresh sharing the next row. Station count stays available on desktop but gives way to
the more useful live state and update time on the narrower screen.

The prediction is an outcome-first `NOW → +30 MIN` decision. Current inventory is the
largest value, the opposite inventory is secondary, and the endpoint uses short rider
language such as “Looks stable”, “May run low” or “May run out”. The visible “Rule
estimate · not validated” safeguard preserves honesty without repeating the risk in a
second label. Compact factors sit below; operational and rule details live in collapsed
disclosures.

When that endpoint is concerning, one cobalt Plan B strip continues the same decision
sequence. It names the closest safer station within two kilometres, shows its current
inventory and outlook, and uses one direct action to select it. A dotted cobalt line on
the map connects the current station to the alternative; its distance is explicitly
straight-line rather than a routing claim.

## Interaction and motion

- A 420 ms cobalt sweep marks the first successful feed load and explicit successful
  refresh. It does not repeat during the automatic refresh cycle or after a failure.
- Buttons use a short two-pixel press response and a cobalt focus state.
- Selecting a station redraws the map and reveals the decision line with a brief,
  functional endpoint animation.
- A newly available Plan B enters from the selected-station side, and choosing it moves
  the map focus and station decision panel to that alternative.
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

Earlier baseline and signature screenshots remain in `docs/assets/` and the existing
evidence directories. They document the iteration that preceded this simplified pass.
Current Plan B desktop and mobile captures are in `evidence-plan-b-final/`; the clean
mobile first-viewport capture is in `evidence-plan-b-mobile-viewport/`.

## Layout refinement

The final layout pass widens the desktop reading frame to 1380 pixels while increasing
the page gutters and section rhythm. Empty and selected states use different spacing:
the map stays close to the controls before selection, while a selected station gets a
clear decision block before the spatial context. Small operational labels were raised
to a readable size rather than compressed to manufacture whitespace.

On mobile, the header is shorter, the forecast is a horizontal Now-to-+30 sequence and
Plan B uses a compact two-column summary above a full-width action. The map legend uses
a stable two-column grid, with an odd final item spanning the row. This keeps the full
station decision and alternative action in the first 390 × 844 viewport without
shrinking touch targets.

## Review record

This simplified pass deliberately removes the patterns that made the previous version
feel generated: the eyebrow, coordinates, numbered section marker, decorative route
rail, large grid field, hard shadows, pastel prediction alerts and repeated prose. The
remaining signature comes from type, paper, exact service data and the single connected
inventory-to-outlook gesture.

The Plan B visual review preserved the compact alternative strip and dotted connection.
It prompted four corrections: the selected decision now precedes the map, mobile helper
copy no longer collides with search, the selected marker uses ink while the alternative
uses cobalt, and search focus follows the cobalt control language. The independent UX
reviewer had no browser surface and made no product claims; the Playwright journey covers
selection, recommendation reading, choosing the alternative and the resulting station
change.

A final independent layout review identified three hierarchy issues: search ownership
was ambiguous on desktop, the mobile header pushed the action below the first screen,
and the wrapped legend could leave one item alone. The final pass groups label and
field, quiets nonessential mobile service metadata, removes repeated forecast wording
and gives the legend a deterministic grid. Responsive evidence for selected and
unselected states is in `evidence-layout-final/`. Full-page selected captures can report
a headless WebGL context loss when the off-screen PyDeck canvas is rasterized; the
visible decision area remains intact and the browser journey separately exercises the
live map.
