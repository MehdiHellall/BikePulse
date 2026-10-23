from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
from numpy.typing import NDArray

from bikepulse.ml.dataset import FEATURES as LEGACY_CORE_FEATURES
from bikepulse.ml.export import EARTH_RADIUS_METERS, NEIGHBOR_RADIUS_METERS

LAG_MINUTES = (5, 10, 15, 30, 45, 60)
WINDOW_MINUTES = (15, 30, 60)
SLOPE_WINDOWS = (10, 15, 30, 60)
NETWORK_LAG_MINUTES = (5, 15, 30)
TARGET_HORIZONS = (10, 15, 20, 30)

CORE_FEATURES = list(LEGACY_CORE_FEATURES)
MULTILAG_DEPLETION_FEATURES = [
    "mechanical_bikes",
    "electric_bikes",
    "mechanical_bike_ratio",
    "electric_bike_ratio",
    *[
        name
        for minutes in LAG_MINUTES
        for name in (
            f"lag_observed_{minutes}m",
            f"lag_bikes_{minutes}m",
            f"lag_bike_ratio_{minutes}m",
            f"bike_delta_{minutes}m",
            f"bike_delta_capacity_{minutes}m",
        )
    ],
    *[
        name
        for minutes in SLOPE_WINDOWS
        for name in (
            f"history_complete_{minutes}m",
            f"bike_slope_{minutes}m",
            f"bike_robust_slope_{minutes}m",
            f"max_depletion_rate_{minutes}m",
            f"min_depletion_rate_{minutes}m",
            f"declining_interval_count_{minutes}m",
            f"declining_interval_fraction_{minutes}m",
            f"consecutive_declining_intervals_{minutes}m",
            f"time_to_zero_{minutes}m",
            f"stock_depletion_interaction_{minutes}m",
        )
    ],
    "depletion_acceleration_15m",
    "station_rebalance_jump_5m",
    "station_rebalance_jump_15m",
    "station_rebalance_jump_30m",
]
VOLATILITY_STATE_FEATURES = [
    *[
        name
        for minutes in WINDOW_MINUTES
        for name in (
            f"inventory_mean_{minutes}m",
            f"inventory_min_{minutes}m",
            f"inventory_max_{minutes}m",
            f"inventory_std_{minutes}m",
            f"inventory_range_{minutes}m",
            f"mean_absolute_change_{minutes}m",
            f"direction_changes_{minutes}m",
            f"fraction_bikes_le_1_{minutes}m",
            f"fraction_bikes_le_2_{minutes}m",
            f"fraction_bikes_le_3_{minutes}m",
        )
    ],
    "occupancy_state",
    "occupancy_state_duration_minutes",
    "minutes_since_empty",
    "empty_episode_count_6h",
    "minutes_since_recovery",
    "previous_empty_episode_minutes",
]
PRIOR_FEATURES = [
    "station_stockout_propensity",
    "station_hour_stockout_propensity",
    "station_typical_bike_ratio",
    "station_time_typical_bike_ratio",
    "bike_ratio_deviation_from_station_time",
]
NETWORK_FEATURES = [
    "nearby_observed_current",
    "nearby_total_bikes",
    "nearby_total_capacity",
    "nearby_local_bike_ratio",
    "nearby_occupancy_deficit",
    "nearby_min_bike_ratio",
    "nearby_mean_bike_ratio",
    "nearby_empty_count",
    "nearby_near_empty_count",
    *[
        name
        for minutes in NETWORK_LAG_MINUTES
        for name in (
            f"nearby_observed_change_{minutes}m",
            f"nearby_signed_bike_change_{minutes}m",
            f"nearby_losing_count_{minutes}m",
            f"nearby_gaining_count_{minutes}m",
            f"nearby_fraction_depleting_{minutes}m",
            f"nearby_weighted_signed_change_{minutes}m",
            f"nearby_depletion_spreading_{minutes}m",
            f"nearby_rebalancing_signature_{minutes}m",
        )
    ],
]

FEATURE_GROUPS = {
    "baseline": CORE_FEATURES,
    "lag_depletion": [*CORE_FEATURES, *MULTILAG_DEPLETION_FEATURES],
    "volatility_state": [
        *CORE_FEATURES,
        *MULTILAG_DEPLETION_FEATURES,
        *VOLATILITY_STATE_FEATURES,
    ],
    "station_priors": [
        *CORE_FEATURES,
        *MULTILAG_DEPLETION_FEATURES,
        *VOLATILITY_STATE_FEATURES,
        *PRIOR_FEATURES,
    ],
    "signed_network": [
        *CORE_FEATURES,
        *MULTILAG_DEPLETION_FEATURES,
        *VOLATILITY_STATE_FEATURES,
        *PRIOR_FEATURES,
        *NETWORK_FEATURES,
    ],
}

FEATURE_AVAILABILITY = {
    "core": (
        "Current published inventory, capacity, exact t-15m change, and calendar values; "
        "all are known at t."
    ),
    "multi_lag_depletion": (
        "Exact t-h observations and trailing differences/slopes ending at t. Missing exact "
        "history remains missing; no forward filling or centered window is used."
    ),
    "volatility_state": (
        "Trailing complete 15/30/60-minute windows and state-machine history updated only after "
        "each observation. Gaps longer than five minutes reset episode continuity."
    ),
    "station_priors": (
        "Fold-specific expanding training encodings. Training rows see only earlier timestamps; "
        "validation rows use frozen training aggregates and never validation outcomes."
    ),
    "signed_network": (
        "Contemporaneous neighbor inventory and exact trailing signed neighbor changes ending at t."
    ),
}


def _validate_grid(grid: pl.DataFrame) -> pl.DataFrame:
    required = {
        "station_id",
        "as_of_utc",
        "received_at_utc",
        "usable",
        "available_bikes",
        "available_docks",
        "mechanical_bikes",
        "electric_bikes",
        "capacity",
        "latitude",
        "longitude",
    }
    if not required.issubset(grid.columns):
        raise ValueError(
            f"Temporal feature grid is missing: {sorted(required - set(grid.columns))}"
        )
    if grid.select(pl.struct("station_id", "as_of_utc").is_duplicated().any()).item():
        raise ValueError("Temporal feature grid contains duplicate station-time keys.")
    if not grid.filter(pl.col("received_at_utc") > pl.col("as_of_utc")).is_empty():
        raise ValueError("A grid observation was received after its prediction boundary.")
    return grid.sort("station_id", "as_of_utc")


def _add_exact_lags(grid: pl.DataFrame) -> pl.DataFrame:
    local = pl.col("as_of_utc").dt.convert_time_zone("Europe/Paris")
    angle = (local.dt.hour() + local.dt.minute() / 60) * (2 * np.pi / 24)
    frame = grid.with_columns(
        (pl.col("available_bikes") / pl.col("capacity")).alias("bike_ratio"),
        (pl.col("mechanical_bikes") / pl.col("capacity")).alias("mechanical_bike_ratio"),
        (pl.col("electric_bikes") / pl.col("capacity")).alias("electric_bike_ratio"),
        angle.sin().alias("hour_sin"),
        angle.cos().alias("hour_cos"),
        (local.dt.weekday() >= 6).cast(pl.Int8).alias("weekend"),
    )
    for minutes in range(5, 61, 5):
        lag = grid.select(
            "station_id",
            (pl.col("as_of_utc") + pl.duration(minutes=minutes)).alias("as_of_utc"),
            pl.col("usable").alias(f"lag_usable_{minutes}m"),
            pl.col("available_bikes").alias(f"raw_lag_bikes_{minutes}m"),
            (pl.col("available_bikes") / pl.col("capacity")).alias(
                f"raw_lag_bike_ratio_{minutes}m"
            ),
        )
        frame = frame.join(lag, on=["station_id", "as_of_utc"], how="left")
        observed = pl.col(f"lag_usable_{minutes}m").fill_null(False)
        frame = frame.with_columns(
            observed.cast(pl.Int8).alias(f"lag_observed_{minutes}m"),
            pl.when(observed)
            .then(pl.col(f"raw_lag_bikes_{minutes}m"))
            .otherwise(None)
            .alias(f"lag_bikes_{minutes}m"),
            pl.when(observed)
            .then(pl.col(f"raw_lag_bike_ratio_{minutes}m"))
            .otherwise(None)
            .alias(f"lag_bike_ratio_{minutes}m"),
        ).drop(
            f"lag_usable_{minutes}m",
            f"raw_lag_bikes_{minutes}m",
            f"raw_lag_bike_ratio_{minutes}m",
        )
        if minutes in LAG_MINUTES:
            frame = frame.with_columns(
                (pl.col("available_bikes") - pl.col(f"lag_bikes_{minutes}m")).alias(
                    f"bike_delta_{minutes}m"
                ),
                (
                    (pl.col("available_bikes") - pl.col(f"lag_bikes_{minutes}m"))
                    / pl.col("capacity")
                ).alias(f"bike_delta_capacity_{minutes}m"),
            )
    return frame.with_columns(pl.col("bike_delta_15m").alias("bike_change_15m"))


def _window_matrix(
    frame: pl.DataFrame, minutes: int
) -> tuple[NDArray[np.float64], NDArray[np.bool_]]:
    columns = ["available_bikes", *[f"lag_bikes_{m}m" for m in range(5, minutes + 1, 5)]]
    values = np.asarray(frame.select(columns).to_numpy(), dtype=np.float64)
    valid = np.isfinite(values) & np.asarray(frame["usable"].to_numpy(), dtype=bool)[:, None]
    return values, valid


def _slope(
    values: NDArray[np.float64], complete: NDArray[np.bool_], minutes: int
) -> NDArray[np.float64]:
    chronological = values[:, ::-1]
    x = np.arange(-minutes, 1, 5, dtype=np.float64)
    centered = x - x.mean()
    result = np.full(values.shape[0], np.nan, dtype=np.float64)
    result[complete] = (chronological[complete] @ centered) / np.square(centered).sum()
    return result


def _add_window_statistics(frame: pl.DataFrame) -> pl.DataFrame:
    columns: list[pl.Series] = []
    slopes: dict[int, NDArray[np.float64]] = {}
    for minutes in SLOPE_WINDOWS:
        values, valid = _window_matrix(frame, minutes)
        complete = valid.all(axis=1)
        chronological = values[:, ::-1]
        changes = np.diff(chronological, axis=1)
        signed_rates = changes / 5.0
        depletion_rates = -signed_rates
        slope = _slope(values, complete, minutes)
        slopes[minutes] = slope
        consecutive = np.zeros(frame.height, dtype=np.float64)
        for offset in range(changes.shape[1] - 1, -1, -1):
            continuing = (changes[:, offset] < 0) & (consecutive == changes.shape[1] - 1 - offset)
            consecutive[continuing] += 1
        ttz = np.full(frame.height, np.nan, dtype=np.float64)
        falling = complete & (slope < 0)
        current = np.asarray(frame["available_bikes"].to_numpy(), dtype=np.float64)
        ttz[falling] = np.clip(current[falling] / -slope[falling], 0.0, 240.0)
        interaction = np.full(frame.height, np.nan, dtype=np.float64)
        interaction[complete] = current[complete] * np.maximum(-slope[complete], 0.0)
        columns.extend(
            [
                pl.Series(f"history_complete_{minutes}m", complete.astype(np.int8)),
                pl.Series(f"bike_slope_{minutes}m", slope),
                pl.Series(
                    f"bike_robust_slope_{minutes}m",
                    np.where(complete, np.median(signed_rates, axis=1), np.nan),
                ),
                pl.Series(
                    f"max_depletion_rate_{minutes}m",
                    np.where(complete, depletion_rates.max(axis=1), np.nan),
                ),
                pl.Series(
                    f"min_depletion_rate_{minutes}m",
                    np.where(complete, depletion_rates.min(axis=1), np.nan),
                ),
                pl.Series(
                    f"declining_interval_count_{minutes}m",
                    np.where(complete, (changes < 0).sum(axis=1), np.nan),
                ),
                pl.Series(
                    f"declining_interval_fraction_{minutes}m",
                    np.where(complete, (changes < 0).mean(axis=1), np.nan),
                ),
                pl.Series(
                    f"consecutive_declining_intervals_{minutes}m",
                    np.where(complete, consecutive, np.nan),
                ),
                pl.Series(f"time_to_zero_{minutes}m", ttz),
                pl.Series(f"stock_depletion_interaction_{minutes}m", interaction),
            ]
        )
        if minutes in WINDOW_MINUTES:
            nonzero_sign = np.sign(changes)
            direction_changes = (
                (nonzero_sign[:, 1:] != nonzero_sign[:, :-1])
                & (nonzero_sign[:, 1:] != 0)
                & (nonzero_sign[:, :-1] != 0)
            ).sum(axis=1)
            columns.extend(
                [
                    pl.Series(
                        f"inventory_mean_{minutes}m",
                        np.where(complete, values.mean(axis=1), np.nan),
                    ),
                    pl.Series(
                        f"inventory_min_{minutes}m",
                        np.where(complete, values.min(axis=1), np.nan),
                    ),
                    pl.Series(
                        f"inventory_max_{minutes}m",
                        np.where(complete, values.max(axis=1), np.nan),
                    ),
                    pl.Series(
                        f"inventory_std_{minutes}m",
                        np.where(complete, values.std(axis=1), np.nan),
                    ),
                    pl.Series(
                        f"inventory_range_{minutes}m",
                        np.where(complete, values.max(axis=1) - values.min(axis=1), np.nan),
                    ),
                    pl.Series(
                        f"mean_absolute_change_{minutes}m",
                        np.where(complete, np.abs(changes).mean(axis=1), np.nan),
                    ),
                    pl.Series(
                        f"direction_changes_{minutes}m",
                        np.where(complete, direction_changes, np.nan),
                    ),
                    *[
                        pl.Series(
                            f"fraction_bikes_le_{limit}_{minutes}m",
                            np.where(complete, (values <= limit).mean(axis=1), np.nan),
                        )
                        for limit in (1, 2, 3)
                    ],
                ]
            )
    prior_values = np.asarray(
        frame.select([f"lag_bikes_{m}m" for m in (15, 20, 25, 30)]).to_numpy(),
        dtype=np.float64,
    )
    prior_complete = np.isfinite(prior_values).all(axis=1)
    prior_slope = _slope(prior_values, prior_complete, 15)
    acceleration = np.where(
        np.isfinite(slopes[15]) & np.isfinite(prior_slope),
        -slopes[15] - (-prior_slope),
        np.nan,
    )
    columns.append(pl.Series("depletion_acceleration_15m", acceleration))
    for minutes in NETWORK_LAG_MINUTES:
        delta = np.asarray(frame[f"bike_delta_{minutes}m"].to_numpy(), dtype=np.float64)
        capacity = np.asarray(frame["capacity"].to_numpy(), dtype=np.float64)
        flag = np.isfinite(delta) & (np.abs(delta) >= np.maximum(5.0, capacity * 0.3))
        columns.append(pl.Series(f"station_rebalance_jump_{minutes}m", flag.astype(np.int8)))
    return frame.with_columns(columns)


def _add_state_duration(frame: pl.DataFrame) -> pl.DataFrame:
    values, valid = _window_matrix(frame, 60)
    capacity = np.asarray(frame["capacity"].to_numpy(), dtype=np.float64)[:, None]
    ratios = np.full_like(values, np.nan)
    np.divide(values, capacity, out=ratios, where=np.isfinite(capacity) & (capacity > 0))
    state = np.where(values <= 3, 0, np.where(ratios >= 0.7, 2, 1))
    current = state[:, 0]
    duration_steps = np.zeros(frame.height, dtype=np.int64)
    active = valid[:, 0].copy()
    for offset in range(1, state.shape[1]):
        active &= valid[:, offset] & (state[:, offset] == current)
        duration_steps[active] += 1
    state_value = np.where(valid[:, 0], current, -1)
    return frame.with_columns(
        pl.Series("occupancy_state", state_value.astype(np.int8)),
        pl.Series("occupancy_state_duration_minutes", (duration_steps * 5).astype(np.int64)),
    )


def _empty_history_columns(frame: pl.DataFrame) -> list[pl.Series]:
    count = frame.height
    minutes_since_empty = np.full(count, np.nan, dtype=np.float64)
    episode_count = np.zeros(count, dtype=np.float64)
    minutes_since_recovery = np.full(count, np.nan, dtype=np.float64)
    previous_duration = np.full(count, np.nan, dtype=np.float64)
    station_ids = frame["station_id"].to_list()
    times: list[datetime] = frame["as_of_utc"].to_list()
    usable = np.asarray(frame["usable"].to_numpy(), dtype=bool)
    bikes = np.asarray(frame["available_bikes"].to_numpy(), dtype=np.float64)
    previous_station: str | None = None
    previous_time: datetime | None = None
    was_empty = False
    episode_start: datetime | None = None
    last_empty: datetime | None = None
    last_recovery: datetime | None = None
    last_duration: float | None = None
    episode_starts: deque[datetime] = deque()
    for index, (station_id, stamp) in enumerate(zip(station_ids, times, strict=True)):
        discontinuity = (
            previous_station != station_id
            or previous_time is None
            or stamp - previous_time != timedelta(minutes=5)
        )
        if discontinuity:
            was_empty = False
            episode_start = None
            last_empty = None
            last_recovery = None
            last_duration = None
            episode_starts.clear()
        if not usable[index]:
            was_empty = False
            episode_start = None
            last_empty = None
            last_recovery = None
            last_duration = None
            episode_starts.clear()
        else:
            while episode_starts and stamp - episode_starts[0] > timedelta(hours=6):
                episode_starts.popleft()
            empty = bikes[index] == 0
            if empty:
                if not was_empty:
                    episode_start = stamp
                    episode_starts.append(stamp)
                last_empty = stamp
                minutes_since_empty[index] = 0.0
            else:
                if was_empty and episode_start is not None:
                    last_recovery = stamp
                    last_duration = (stamp - episode_start).total_seconds() / 60.0
                if last_empty is not None:
                    minutes_since_empty[index] = (stamp - last_empty).total_seconds() / 60.0
                if last_recovery is not None:
                    minutes_since_recovery[index] = (stamp - last_recovery).total_seconds() / 60.0
                if last_duration is not None:
                    previous_duration[index] = last_duration
            episode_count[index] = len(episode_starts)
            was_empty = empty
        previous_station = station_id
        previous_time = stamp
    return [
        pl.Series("minutes_since_empty", minutes_since_empty),
        pl.Series("empty_episode_count_6h", episode_count),
        pl.Series("minutes_since_recovery", minutes_since_recovery),
        pl.Series("previous_empty_episode_minutes", previous_duration),
    ]


def build_temporal_features(grid: pl.DataFrame) -> pl.DataFrame:
    """Create auditable station-history features using exact observations at or before t."""
    frame = _add_exact_lags(_validate_grid(grid))
    frame = _add_window_statistics(frame)
    frame = _add_state_duration(frame)
    frame = frame.with_columns(_empty_history_columns(frame))
    return frame.sort("as_of_utc", "station_id")


def build_target_labels(grid: pl.DataFrame) -> pl.DataFrame:
    """Build exact future labels while leaving any incomplete future window null."""
    frame = grid.select("station_id", "as_of_utc", "usable", "available_bikes")
    future_columns: list[str] = []
    for minutes in range(5, 31, 5):
        usable_name = f"future_usable_{minutes}m"
        bikes_name = f"future_bikes_{minutes}m"
        future = grid.select(
            "station_id",
            (pl.col("as_of_utc") - pl.duration(minutes=minutes)).alias("as_of_utc"),
            pl.col("usable").alias(usable_name),
            pl.col("available_bikes").alias(bikes_name),
        )
        frame = frame.join(future, on=["station_id", "as_of_utc"], how="left")
        future_columns.extend([usable_name, bikes_name])
    eligible = pl.col("usable") & (pl.col("available_bikes") > 0)
    expressions: list[pl.Expr] = []
    for horizon in TARGET_HORIZONS:
        points = range(5, horizon + 1, 5)
        complete = pl.all_horizontal(
            pl.col(f"future_usable_{minutes}m").fill_null(False) for minutes in points
        )
        event = pl.any_horizontal(pl.col(f"future_bikes_{minutes}m") == 0 for minutes in points)
        expressions.extend(
            [
                pl.when(eligible & complete)
                .then(event.cast(pl.Int8))
                .otherwise(pl.lit(None, dtype=pl.Int8))
                .alias(f"label_stockout_{horizon}m"),
                (pl.col("as_of_utc") + pl.duration(minutes=horizon)).alias(
                    f"label_end_stockout_{horizon}m"
                ),
            ]
        )
    complete_30m = pl.all_horizontal(
        pl.col(f"future_usable_{minutes}m").fill_null(False) for minutes in range(5, 31, 5)
    )
    sustained = pl.any_horizontal(
        (pl.col(f"future_bikes_{minutes}m") == 0) & (pl.col(f"future_bikes_{minutes + 5}m") == 0)
        for minutes in range(5, 30, 5)
    )
    expressions.extend(
        [
            pl.when(eligible & complete_30m)
            .then(sustained.cast(pl.Int8))
            .otherwise(pl.lit(None, dtype=pl.Int8))
            .alias("label_sustained_30m"),
            (pl.col("as_of_utc") + pl.duration(minutes=30)).alias("label_end_sustained_30m"),
        ]
    )
    return frame.with_columns(expressions).select(
        "station_id",
        "as_of_utc",
        *[
            name
            for horizon in TARGET_HORIZONS
            for name in (f"label_stockout_{horizon}m", f"label_end_stockout_{horizon}m")
        ],
        "label_sustained_30m",
        "label_end_sustained_30m",
    )


def _neighbor_pairs(frame: pl.DataFrame, radius_meters: float) -> pl.DataFrame:
    positions = (
        frame.select("station_id", "latitude", "longitude")
        .filter(pl.col("latitude").is_not_null() & pl.col("longitude").is_not_null())
        .unique()
        .sort("station_id", "latitude", "longitude")
    )
    if positions.is_empty():
        return pl.DataFrame(
            schema={
                "target_station_id": pl.String,
                "target_latitude": pl.Float64,
                "target_longitude": pl.Float64,
                "neighbor_station_id": pl.String,
                "neighbor_latitude": pl.Float64,
                "neighbor_longitude": pl.Float64,
                "distance_weight": pl.Float64,
            }
        )
    latitude = np.radians(positions["latitude"].to_numpy())
    longitude = np.radians(positions["longitude"].to_numpy())
    delta_latitude = latitude[:, None] - latitude[None, :]
    delta_longitude = longitude[:, None] - longitude[None, :]
    haversine = np.sin(delta_latitude / 2) ** 2 + (
        np.cos(latitude[:, None]) * np.cos(latitude[None, :]) * np.sin(delta_longitude / 2) ** 2
    )
    distances = EARTH_RADIUS_METERS * 2 * np.arcsin(np.sqrt(np.clip(haversine, 0, 1)))
    station_ids = positions["station_id"].to_numpy()
    target, neighbor = np.where(
        (distances <= radius_meters) & (station_ids[:, None] != station_ids[None, :])
    )
    latitudes = positions["latitude"].to_numpy()
    longitudes = positions["longitude"].to_numpy()
    return pl.DataFrame(
        {
            "target_station_id": station_ids[target].tolist(),
            "target_latitude": latitudes[target].tolist(),
            "target_longitude": longitudes[target].tolist(),
            "neighbor_station_id": station_ids[neighbor].tolist(),
            "neighbor_latitude": latitudes[neighbor].tolist(),
            "neighbor_longitude": longitudes[neighbor].tolist(),
            "distance_weight": np.exp(-distances[target, neighbor] / radius_meters).tolist(),
        }
    )


def _network_batch(frame: pl.DataFrame, pairs: pl.DataFrame) -> pl.DataFrame:
    targets = frame.select(
        "as_of_utc",
        pl.col("station_id").alias("target_station_id"),
        pl.col("latitude").alias("target_latitude"),
        pl.col("longitude").alias("target_longitude"),
    )
    neighbors = frame.select(
        "as_of_utc",
        pl.col("station_id").alias("neighbor_station_id"),
        pl.col("latitude").alias("neighbor_latitude"),
        pl.col("longitude").alias("neighbor_longitude"),
        pl.col("usable").alias("neighbor_usable"),
        pl.col("available_bikes").alias("neighbor_bikes"),
        pl.col("capacity").alias("neighbor_capacity"),
        pl.col("bike_ratio").alias("neighbor_bike_ratio"),
        *[
            pl.col(f"lag_observed_{minutes}m").alias(f"neighbor_lag_observed_{minutes}m")
            for minutes in NETWORK_LAG_MINUTES
        ],
        *[
            pl.col(f"bike_delta_{minutes}m").alias(f"neighbor_delta_{minutes}m")
            for minutes in NETWORK_LAG_MINUTES
        ],
    )
    joined = targets.join(
        pairs,
        on=["target_station_id", "target_latitude", "target_longitude"],
        how="left",
    ).join(
        neighbors,
        on=[
            "as_of_utc",
            "neighbor_station_id",
            "neighbor_latitude",
            "neighbor_longitude",
        ],
        how="left",
    )
    usable = pl.col("neighbor_usable").fill_null(False)
    aggregate_expressions: list[pl.Expr] = [
        usable.cast(pl.Int64).sum().alias("nearby_observed_current"),
        pl.when(usable)
        .then(pl.col("neighbor_bikes"))
        .otherwise(0)
        .sum()
        .alias("nearby_total_bikes"),
        pl.when(usable)
        .then(pl.col("neighbor_capacity"))
        .otherwise(0)
        .sum()
        .alias("nearby_total_capacity"),
        pl.when(usable)
        .then(pl.col("neighbor_capacity") - pl.col("neighbor_bikes"))
        .otherwise(0)
        .sum()
        .alias("nearby_occupancy_deficit"),
        pl.when(usable)
        .then(pl.col("neighbor_bike_ratio"))
        .otherwise(None)
        .min()
        .alias("nearby_min_bike_ratio"),
        pl.when(usable)
        .then(pl.col("neighbor_bike_ratio"))
        .otherwise(None)
        .mean()
        .alias("nearby_mean_bike_ratio"),
        (usable & (pl.col("neighbor_bikes") == 0)).cast(pl.Int64).sum().alias("nearby_empty_count"),
        (usable & (pl.col("neighbor_bikes") <= 3))
        .cast(pl.Int64)
        .sum()
        .alias("nearby_near_empty_count"),
    ]
    for minutes in NETWORK_LAG_MINUTES:
        delta = pl.col(f"neighbor_delta_{minutes}m")
        observed = usable & (pl.col(f"neighbor_lag_observed_{minutes}m").fill_null(0) == 1)
        large = observed & (
            delta.abs() >= pl.max_horizontal(pl.lit(5.0), pl.col("neighbor_capacity") * 0.3)
        )
        aggregate_expressions.extend(
            [
                observed.cast(pl.Int64).sum().alias(f"nearby_observed_change_{minutes}m"),
                pl.when(observed)
                .then(delta)
                .otherwise(0)
                .sum()
                .alias(f"nearby_signed_bike_change_{minutes}m"),
                (observed & (delta < 0))
                .cast(pl.Int64)
                .sum()
                .alias(f"nearby_losing_count_{minutes}m"),
                (observed & (delta > 0))
                .cast(pl.Int64)
                .sum()
                .alias(f"nearby_gaining_count_{minutes}m"),
                pl.when(observed)
                .then(delta * pl.col("distance_weight"))
                .otherwise(0)
                .sum()
                .alias(f"nearby_weighted_signed_change_{minutes}m"),
                (large & (delta < 0))
                .cast(pl.Int64)
                .sum()
                .alias(f"nearby_large_loss_count_{minutes}m"),
                (large & (delta > 0))
                .cast(pl.Int64)
                .sum()
                .alias(f"nearby_large_gain_count_{minutes}m"),
            ]
        )
    aggregate = joined.group_by("as_of_utc", "target_station_id").agg(aggregate_expressions)
    derived: list[pl.Expr] = [
        pl.when(pl.col("nearby_total_capacity") > 0)
        .then(pl.col("nearby_total_bikes") / pl.col("nearby_total_capacity"))
        .otherwise(None)
        .alias("nearby_local_bike_ratio")
    ]
    for minutes in NETWORK_LAG_MINUTES:
        observed_name = f"nearby_observed_change_{minutes}m"
        loss_name = f"nearby_losing_count_{minutes}m"
        signed_name = f"nearby_signed_bike_change_{minutes}m"
        large_loss = f"nearby_large_loss_count_{minutes}m"
        large_gain = f"nearby_large_gain_count_{minutes}m"
        large_count = pl.col(large_loss) + pl.col(large_gain)
        fraction = (
            pl.when(pl.col(observed_name) > 0)
            .then(pl.col(loss_name) / pl.col(observed_name))
            .otherwise(None)
        )
        derived.extend(
            [
                fraction.alias(f"nearby_fraction_depleting_{minutes}m"),
                ((pl.col(observed_name) >= 3) & (fraction >= 0.5) & (pl.col(signed_name) < 0))
                .cast(pl.Int8)
                .alias(f"nearby_depletion_spreading_{minutes}m"),
                (
                    (large_count >= 2)
                    & (
                        pl.max_horizontal(pl.col(large_loss), pl.col(large_gain)) / large_count
                        >= 0.6
                    )
                )
                .fill_null(False)
                .cast(pl.Int8)
                .alias(f"nearby_rebalancing_signature_{minutes}m"),
            ]
        )
    return (
        aggregate.with_columns(derived)
        .drop(
            *[
                name
                for minutes in NETWORK_LAG_MINUTES
                for name in (
                    f"nearby_large_loss_count_{minutes}m",
                    f"nearby_large_gain_count_{minutes}m",
                )
            ]
        )
        .rename({"target_station_id": "station_id"})
        .select("station_id", "as_of_utc", *NETWORK_FEATURES)
    )


def build_signed_network_features(
    frame: pl.DataFrame,
    *,
    radius_meters: float = NEIGHBOR_RADIUS_METERS,
    batch_timestamp_count: int = 12,
) -> pl.DataFrame:
    """Aggregate signed, past-only movement for other stations within the radius."""
    if batch_timestamp_count < 1:
        raise ValueError("Network feature batches must contain at least one timestamp.")
    pairs = _neighbor_pairs(frame, radius_meters)
    times = frame["as_of_utc"].unique().sort().to_list()
    batches = [
        _network_batch(
            frame.filter(pl.col("as_of_utc").is_in(times[offset : offset + batch_timestamp_count])),
            pairs,
        )
        for offset in range(0, len(times), batch_timestamp_count)
    ]
    if not batches:
        return pl.DataFrame(
            schema={
                "station_id": pl.String,
                "as_of_utc": pl.Datetime("us", "UTC"),
                **dict.fromkeys(NETWORK_FEATURES, pl.Float64),
            }
        )
    return pl.concat(batches, how="vertical_relaxed").sort("as_of_utc", "station_id")


def _time_bucket(stamp: datetime) -> int:
    local = stamp.astimezone(ZoneInfo("Europe/Paris"))
    return local.hour // 4


def add_fold_station_priors(
    train: pl.DataFrame,
    validation: pl.DataFrame,
    *,
    label_column: str,
    label_end_column: str,
    validation_start: datetime,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Encode training rows from earlier rows and validation rows from frozen training only."""
    train = train.sort("as_of_utc", "station_id")
    validation = validation.sort("as_of_utc", "station_id")
    if not train.filter(pl.col(label_end_column) >= validation_start).is_empty():
        raise ValueError("Training labels are not fully known before validation starts.")

    global_positive = 0
    global_labels = 0
    station_labels: dict[str, list[int]] = {}
    station_hour_labels: dict[tuple[str, int], list[int]] = {}
    global_ratio_sum = 0.0
    global_ratio_count = 0
    station_ratios: dict[str, list[float]] = {}
    station_time_ratios: dict[tuple[str, int], list[float]] = {}

    def encoded(
        station: str, bucket: int, ratio: float
    ) -> tuple[float, float, float, float, float]:
        global_propensity = (global_positive + 1.0) / (global_labels + 20.0)
        station_stat = station_labels.get(station, [0, 0])
        station_propensity = (station_stat[0] + 50.0 * global_propensity) / (station_stat[1] + 50.0)
        station_hour_stat = station_hour_labels.get((station, bucket), [0, 0])
        station_hour_propensity = (station_hour_stat[0] + 20.0 * station_propensity) / (
            station_hour_stat[1] + 20.0
        )
        global_ratio = global_ratio_sum / global_ratio_count if global_ratio_count else 0.5
        station_ratio_stat = station_ratios.get(station, [0.0, 0.0])
        station_ratio = (station_ratio_stat[0] + 20.0 * global_ratio) / (
            station_ratio_stat[1] + 20.0
        )
        station_time_stat = station_time_ratios.get((station, bucket), [0.0, 0.0])
        station_time_ratio = (station_time_stat[0] + 10.0 * station_ratio) / (
            station_time_stat[1] + 10.0
        )
        return (
            station_propensity,
            station_hour_propensity,
            station_ratio,
            station_time_ratio,
            ratio - station_time_ratio,
        )

    train_ids = train["station_id"].to_list()
    train_times: list[datetime] = train["as_of_utc"].to_list()
    train_label_ends: list[datetime] = train[label_end_column].to_list()
    train_labels = np.asarray(train[label_column].to_numpy(), dtype=np.int64)
    train_ratios = np.asarray(train["bike_ratio"].to_numpy(), dtype=np.float64)
    output = np.empty((train.height, len(PRIOR_FEATURES)), dtype=np.float64)
    pending: deque[tuple[datetime, str, int, int]] = deque()
    start = 0
    while start < train.height:
        stamp = train_times[start]
        while pending and pending[0][0] <= stamp:
            _, station, bucket, label = pending.popleft()
            global_positive += label
            global_labels += 1
            station_stat = station_labels.setdefault(station, [0, 0])
            station_stat[0] += label
            station_stat[1] += 1
            station_hour_stat = station_hour_labels.setdefault((station, bucket), [0, 0])
            station_hour_stat[0] += label
            station_hour_stat[1] += 1
        end = start + 1
        while end < train.height and train_times[end] == stamp:
            end += 1
        for index in range(start, end):
            bucket = _time_bucket(train_times[index])
            output[index] = encoded(train_ids[index], bucket, train_ratios[index])
        for index in range(start, end):
            station = train_ids[index]
            bucket = _time_bucket(train_times[index])
            ratio = train_ratios[index]
            global_ratio_sum += ratio
            global_ratio_count += 1
            station_ratio_stat = station_ratios.setdefault(station, [0.0, 0.0])
            station_ratio_stat[0] += ratio
            station_ratio_stat[1] += 1.0
            station_time_stat = station_time_ratios.setdefault((station, bucket), [0.0, 0.0])
            station_time_stat[0] += ratio
            station_time_stat[1] += 1.0
            pending.append((train_label_ends[index], station, bucket, int(train_labels[index])))
        start = end
    while pending and pending[0][0] <= validation_start:
        _, station, bucket, label = pending.popleft()
        global_positive += label
        global_labels += 1
        station_stat = station_labels.setdefault(station, [0, 0])
        station_stat[0] += label
        station_stat[1] += 1
        station_hour_stat = station_hour_labels.setdefault((station, bucket), [0, 0])
        station_hour_stat[0] += label
        station_hour_stat[1] += 1
    validation_output = np.asarray(
        [
            encoded(station, _time_bucket(stamp), ratio)
            for station, stamp, ratio in zip(
                validation["station_id"].to_list(),
                validation["as_of_utc"].to_list(),
                np.asarray(validation["bike_ratio"].to_numpy(), dtype=np.float64),
                strict=True,
            )
        ],
        dtype=np.float64,
    )
    train_columns = [pl.Series(name, output[:, index]) for index, name in enumerate(PRIOR_FEATURES)]
    validation_columns = [
        pl.Series(name, validation_output[:, index]) for index, name in enumerate(PRIOR_FEATURES)
    ]
    return train.with_columns(train_columns), validation.with_columns(validation_columns)


def feature_quality_summary(frame: pl.DataFrame) -> dict[str, Any]:
    times: list[datetime] = frame["as_of_utc"].unique().sort().to_list()
    gaps = [
        {
            "start_utc": previous.isoformat(),
            "end_utc": current.isoformat(),
            "minutes": (current - previous).total_seconds() / 60.0,
        }
        for previous, current in pairwise(times)
        if current - previous > timedelta(minutes=5)
    ]
    return {
        "row_count": frame.height,
        "timestamp_count": len(times),
        "station_count": frame["station_id"].n_unique(),
        "first_timestamp_utc": times[0].isoformat() if times else None,
        "last_timestamp_utc": times[-1].isoformat() if times else None,
        "timestamp_gaps": gaps,
        "complete_history_rates": {
            f"{minutes}m": _column_mean(frame, f"history_complete_{minutes}m")
            for minutes in SLOPE_WINDOWS
        },
    }


def _column_mean(frame: pl.DataFrame, column: str) -> float:
    value: Any = frame[column].mean()
    return float(value or 0.0)
