from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Literal

from bikepulse.config.settings import MovementSettings

AvailabilityMode = Literal["bikes", "docks"]
MovementDirection = Literal["falling", "rising", "stable", "mixed"]
MovementStatus = Literal["estimated", "insufficient", "stale", "invalid", "rebalancing"]
MIN_DIRECTION_CONSISTENCY = 0.6
FORECAST_MINUTES = 30


@dataclass(frozen=True)
class StationObservation:
    station_id: str
    reported_at_utc: datetime
    capacity: int
    available_bikes: int | None
    available_docks: int | None
    is_installed: bool | None
    is_renting: bool | None
    is_returning: bool | None


@dataclass(frozen=True)
class InventoryMovement:
    direction: MovementDirection
    rate_per_minute: float
    consistency: float
    projected_count: float
    observation_count: int
    span_minutes: float


@dataclass(frozen=True)
class MovementAnalysis:
    status: MovementStatus
    estimate: InventoryMovement | None
    reason: str


class StationObservationTracker:
    """Thread-safe, bounded station observations kept only in process memory."""

    def __init__(self, settings: MovementSettings) -> None:
        self._settings = settings
        self._window = timedelta(seconds=settings.observation_window_seconds)
        self._histories: dict[str, deque[StationObservation]] = {}
        self._lock = RLock()

    def record_batch(
        self,
        observations: Iterable[StationObservation],
        *,
        active_station_ids: set[str],
        observed_at_utc: datetime,
    ) -> None:
        reference = observed_at_utc.astimezone(UTC)
        cutoff = reference - self._window
        with self._lock:
            for station_id in tuple(self._histories):
                if station_id not in active_station_ids:
                    del self._histories[station_id]
                    continue
                self._prune_history(station_id, cutoff)

            for observation in observations:
                timestamp = observation.reported_at_utc
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    continue
                timestamp = timestamp.astimezone(UTC)
                if timestamp < cutoff or timestamp > reference:
                    continue
                normalized = StationObservation(
                    station_id=observation.station_id,
                    reported_at_utc=timestamp,
                    capacity=observation.capacity,
                    available_bikes=observation.available_bikes,
                    available_docks=observation.available_docks,
                    is_installed=observation.is_installed,
                    is_renting=observation.is_renting,
                    is_returning=observation.is_returning,
                )
                history = self._histories.setdefault(
                    observation.station_id,
                    deque(maxlen=self._settings.max_observations_per_station),
                )
                if history and timestamp <= history[-1].reported_at_utc:
                    continue
                history.append(normalized)

    def observations_for(
        self, station_id: str, *, now_utc: datetime
    ) -> tuple[StationObservation, ...]:
        cutoff = now_utc.astimezone(UTC) - self._window
        with self._lock:
            self._prune_history(station_id, cutoff)
            return tuple(self._histories.get(station_id, ()))

    def _prune_history(self, station_id: str, cutoff: datetime) -> None:
        history = self._histories.get(station_id)
        if history is None:
            return
        while history and history[0].reported_at_utc < cutoff:
            history.popleft()
        if not history:
            del self._histories[station_id]


def _count(observation: StationObservation, mode: AvailabilityMode) -> int | None:
    return observation.available_bikes if mode == "bikes" else observation.available_docks


def _is_usable(
    observation: StationObservation,
    *,
    mode: AvailabilityMode,
    capacity: int,
) -> bool:
    count = _count(observation, mode)
    operating = observation.is_renting if mode == "bikes" else observation.is_returning
    return (
        observation.capacity == capacity
        and count is not None
        and 0 <= count <= capacity
        and observation.is_installed is True
        and operating is True
    )


def _is_rebalancing_jump(
    left: StationObservation,
    right: StationObservation,
    *,
    mode: AvailabilityMode,
    capacity: int,
    settings: MovementSettings,
) -> bool:
    left_count = _count(left, mode)
    right_count = _count(right, mode)
    if left_count is None or right_count is None or capacity <= 0:
        return False
    change = abs(right_count - left_count)
    return (
        change >= settings.rebalancing_min_count
        and change / capacity >= settings.rebalancing_min_capacity_share
    )


def _weighted_median(values: Sequence[tuple[float, int]]) -> float:
    ordered = sorted(values, key=lambda item: item[0])
    total_weight = sum(weight for _, weight in ordered)
    midpoint = total_weight / 2
    accumulated = 0
    for value, weight in ordered:
        accumulated += weight
        if accumulated >= midpoint:
            return value
    return ordered[-1][0]


def analyze_inventory_movement(
    observations: Sequence[StationObservation],
    *,
    mode: AvailabilityMode,
    current_count: int,
    capacity: int,
    now_utc: datetime,
    settings: MovementSettings,
) -> MovementAnalysis:
    """Estimate a robust, recent inventory slope from a bounded observation sequence."""
    if capacity <= 0 or not 0 <= current_count <= capacity:
        return MovementAnalysis("invalid", None, "Current inventory cannot support a trend.")

    now = now_utc.astimezone(UTC)
    cutoff = now - timedelta(seconds=settings.observation_window_seconds)
    ordered = sorted(
        (
            observation
            for observation in observations
            if observation.reported_at_utc.tzinfo is not None
            and observation.reported_at_utc.utcoffset() is not None
            and cutoff <= observation.reported_at_utc.astimezone(UTC) <= now
        ),
        key=lambda observation: observation.reported_at_utc,
    )
    distinct: list[StationObservation] = []
    for observation in ordered:
        if distinct and observation.reported_at_utc <= distinct[-1].reported_at_utc:
            continue
        distinct.append(observation)

    usable: list[StationObservation] = []
    rebalancing_seen = False
    for observation in distinct:
        if not _is_usable(observation, mode=mode, capacity=capacity):
            usable = []
            continue
        if usable and _is_rebalancing_jump(
            usable[-1],
            observation,
            mode=mode,
            capacity=capacity,
            settings=settings,
        ):
            rebalancing_seen = True
            usable = [observation]
            continue
        usable.append(observation)

    if not usable:
        return MovementAnalysis(
            "insufficient", None, "No trustworthy recent movement is available."
        )
    newest = usable[-1]
    newest_count = _count(newest, mode)
    if newest_count != current_count:
        return MovementAnalysis(
            "invalid",
            None,
            "Recent movement does not match the current inventory.",
        )
    if (now - newest.reported_at_utc.astimezone(UTC)).total_seconds() > (
        settings.max_sample_age_seconds
    ):
        return MovementAnalysis("stale", None, "Recent movement is too old to use.")
    if len(usable) < settings.min_observations:
        status: MovementStatus = "rebalancing" if rebalancing_seen else "insufficient"
        reason = (
            "A probable rebalancing change was excluded from recent movement."
            if rebalancing_seen
            else "Not enough distinct recent reports are available for movement."
        )
        return MovementAnalysis(status, None, reason)

    span_seconds = (usable[-1].reported_at_utc - usable[0].reported_at_utc).total_seconds()
    if span_seconds < settings.min_span_seconds:
        return MovementAnalysis(
            "insufficient",
            None,
            "Recent reports do not yet cover a long enough period for movement.",
        )

    pair_slopes: list[tuple[float, int]] = []
    for left_index, left in enumerate(usable[:-1]):
        left_count = _count(left, mode)
        assert left_count is not None
        for right_index in range(left_index + 1, len(usable)):
            right = usable[right_index]
            right_count = _count(right, mode)
            assert right_count is not None
            minutes = (right.reported_at_utc - left.reported_at_utc).total_seconds() / 60
            if minutes > 0:
                pair_slopes.append(((right_count - left_count) / minutes, right_index + 1))
    if not pair_slopes:
        return MovementAnalysis("insufficient", None, "Recent report times cannot form a trend.")

    rate = _weighted_median(pair_slopes)
    if rate <= -settings.stable_rate_per_minute:
        direction: MovementDirection = "falling"
    elif rate >= settings.stable_rate_per_minute:
        direction = "rising"
    else:
        direction = "stable"

    matching_weight = 0
    total_weight = 0
    for index in range(1, len(usable)):
        left_count = _count(usable[index - 1], mode)
        right_count = _count(usable[index], mode)
        assert left_count is not None and right_count is not None
        minutes = (
            usable[index].reported_at_utc - usable[index - 1].reported_at_utc
        ).total_seconds() / 60
        interval_rate = (right_count - left_count) / minutes
        weight = index + 1
        total_weight += weight
        if (
            (direction == "falling" and interval_rate <= -settings.stable_rate_per_minute)
            or (direction == "rising" and interval_rate >= settings.stable_rate_per_minute)
            or (direction == "stable" and abs(interval_rate) < settings.stable_rate_per_minute)
        ):
            matching_weight += weight
    consistency = matching_weight / total_weight if total_weight else 0
    if consistency < MIN_DIRECTION_CONSISTENCY:
        direction = "mixed"

    projected = min(capacity, max(0.0, current_count + rate * FORECAST_MINUTES))
    movement = InventoryMovement(
        direction=direction,
        rate_per_minute=rate,
        consistency=consistency,
        projected_count=projected,
        observation_count=len(usable),
        span_minutes=span_seconds / 60,
    )
    return MovementAnalysis("estimated", movement, "Recent movement is usable.")
