from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bikepulse.config.settings import MovementSettings
from bikepulse.prediction.movement import (
    StationObservation,
    StationObservationTracker,
    analyze_inventory_movement,
)

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def observation(
    minute: int,
    bikes: int,
    docks: int,
    *,
    station_id: str = "1",
    capacity: int = 20,
    is_installed: bool = True,
    is_renting: bool = True,
    is_returning: bool = True,
) -> StationObservation:
    return StationObservation(
        station_id=station_id,
        reported_at_utc=NOW + timedelta(minutes=minute),
        capacity=capacity,
        available_bikes=bikes,
        available_docks=docks,
        is_installed=is_installed,
        is_renting=is_renting,
        is_returning=is_returning,
    )


def analyze(
    observations: list[StationObservation],
    *,
    mode: str = "bikes",
    current_count: int | None = None,
    capacity: int = 20,
    now_minute: int = 15,
    settings: MovementSettings | None = None,
):
    latest = observations[-1]
    if current_count is None:
        current_count = latest.available_bikes if mode == "bikes" else latest.available_docks
    assert current_count is not None
    return analyze_inventory_movement(
        observations,
        mode="docks" if mode == "docks" else "bikes",
        current_count=current_count,
        capacity=capacity,
        now_utc=NOW + timedelta(minutes=now_minute),
        settings=settings or MovementSettings(),
    )


def test_tracker_deduplicates_orders_prunes_and_enforces_hard_limit() -> None:
    settings = MovementSettings(max_observations_per_station=3)
    tracker = StationObservationTracker(settings)
    for item in (
        observation(0, 10, 10),
        observation(1, 9, 11),
        observation(1, 2, 18),
        observation(0, 8, 12),
        observation(2, 8, 12),
        observation(3, 7, 13),
    ):
        tracker.record_batch(
            (item,),
            active_station_ids={"1"},
            observed_at_utc=item.reported_at_utc,
        )

    retained = tracker.observations_for("1", now_utc=NOW + timedelta(minutes=3))
    assert [(item.reported_at_utc.minute, item.available_bikes) for item in retained] == [
        (1, 9),
        (2, 8),
        (3, 7),
    ]

    tracker.record_batch(
        (observation(4, 5, 15, station_id="2"),),
        active_station_ids={"2"},
        observed_at_utc=NOW + timedelta(minutes=4),
    )
    assert tracker.observations_for("1", now_utc=NOW + timedelta(minutes=4)) == ()
    assert len(tracker.observations_for("2", now_utc=NOW + timedelta(minutes=4))) == 1

    expired_at = NOW + timedelta(minutes=24, seconds=1)
    tracker.record_batch((), active_station_ids={"2"}, observed_at_utc=expired_at)
    assert tracker.observations_for("2", now_utc=expired_at) == ()


@pytest.mark.parametrize(
    "counts,expected_direction",
    [
        ([10, 8, 6, 4], "falling"),
        ([4, 6, 8, 10], "rising"),
        ([10, 10, 10, 10], "stable"),
    ],
)
def test_movement_direction_uses_recent_station_reports(counts, expected_direction) -> None:
    observations = [observation(index * 5, count, 20 - count) for index, count in enumerate(counts)]
    result = analyze(observations)
    assert result.status == "estimated"
    assert result.estimate is not None
    assert result.estimate.direction == expected_direction
    assert result.estimate.observation_count == 4
    assert result.estimate.span_minutes == 15


def test_robust_slope_resists_one_noisy_reading_and_weights_recent_direction() -> None:
    counts = [20, 23, 19, 18, 17]
    observations = [
        observation(index * 4, count, 50 - count, capacity=50) for index, count in enumerate(counts)
    ]
    result = analyze(observations, capacity=50, now_minute=16)
    assert result.estimate is not None
    assert result.estimate.direction == "falling"
    assert result.estimate.rate_per_minute == pytest.approx(-0.25)
    assert result.estimate.consistency >= 0.6


def test_bikes_and_docks_have_independent_directions() -> None:
    observations = [
        observation(0, 10, 5),
        observation(5, 8, 7),
        observation(10, 6, 9),
        observation(15, 4, 11),
    ]
    bikes = analyze(observations, mode="bikes")
    docks = analyze(observations, mode="docks", current_count=11)
    assert bikes.estimate is not None and bikes.estimate.direction == "falling"
    assert docks.estimate is not None and docks.estimate.direction == "rising"


def test_probable_rebalancing_jump_resets_the_trend() -> None:
    observations = [
        observation(0, 10, 10),
        observation(5, 9, 11),
        observation(10, 18, 2),
    ]
    result = analyze(observations, current_count=18, now_minute=10)
    assert result.status == "rebalancing"
    assert result.estimate is None
    assert "rebalancing" in result.reason


def test_insufficient_stale_and_operating_gap_fall_back_without_a_trend() -> None:
    insufficient = analyze([observation(10, 8, 12), observation(15, 7, 13)])
    assert insufficient.status == "insufficient"

    stale_settings = MovementSettings(max_sample_age_seconds=60)
    stale = analyze(
        [observation(0, 10, 10), observation(5, 9, 11), observation(10, 8, 12)],
        current_count=8,
        now_minute=15,
        settings=stale_settings,
    )
    assert stale.status == "stale"

    gap = analyze(
        [
            observation(0, 10, 10),
            observation(5, 9, 11, is_renting=False),
            observation(10, 8, 12),
        ],
        current_count=8,
        now_minute=10,
    )
    assert gap.status == "insufficient"


def test_projection_is_clamped_to_station_capacity() -> None:
    observations = [observation(0, 2, 18), observation(5, 8, 12), observation(10, 14, 6)]
    result = analyze(observations, current_count=14, now_minute=10)
    assert result.estimate is not None
    assert result.estimate.projected_count == 20


def test_invalid_current_inventory_cannot_produce_movement() -> None:
    result = analyze(
        [observation(0, 10, 10), observation(5, 9, 11), observation(10, 8, 12)],
        current_count=21,
        now_minute=10,
    )
    assert result.status == "invalid"
    assert result.estimate is None
