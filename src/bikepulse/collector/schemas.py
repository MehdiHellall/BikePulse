from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator


class RecordError(BaseModel):
    station_id: str
    code: str
    message: str


class StationStatusRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    station_id: str
    is_installed: int = Field(ge=0, le=1)
    is_renting: int = Field(ge=0, le=1)
    is_returning: int = Field(ge=0, le=1)
    num_bikes_available: int = Field(ge=0)
    num_bikes_available_types: list[dict[str, int]] = Field(default_factory=list)
    num_docks_available: int = Field(ge=0)
    last_reported: int = Field(ge=0)

    @field_validator("station_id", mode="before")
    @classmethod
    def station_id_as_string(cls, value: object) -> str:
        return str(value)


class StationInformationRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    station_id: str
    name: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    capacity: int = Field(ge=0)

    @field_validator("station_id", mode="before")
    @classmethod
    def station_id_as_string(cls, value: object) -> str:
        return str(value)


class GbfsPayload(BaseModel):
    last_updated: int = Field(
        ge=0,
        validation_alias=AliasChoices("last_updated", "lastUpdatedOther"),
    )
    ttl: int = Field(ge=0)
    data: dict[str, Any]

    @field_validator("data")
    @classmethod
    def data_has_stations(cls, value: dict[str, Any]) -> dict[str, Any]:
        if "stations" not in value or not isinstance(value["stations"], list):
            raise ValueError("GBFS payload must contain data.stations")
        return value


class NormalizedStatus(BaseModel):
    snapshot_id: str
    collected_at_utc: datetime
    feed_updated_at_utc: datetime
    station_id: str
    station_last_reported_utc: datetime
    data_age_seconds: int
    is_installed: bool
    is_renting: bool
    is_returning: bool
    available_bikes: int
    mechanical_bikes: int
    electric_bikes: int
    available_docks: int


class NormalizedInformation(BaseModel):
    station_id: str
    name: str
    latitude: float
    longitude: float
    capacity: int


class NormalizedBatch(BaseModel):
    kind: Literal["status", "information"]
    rows: list[NormalizedStatus] | list[NormalizedInformation]
    errors: list[RecordError]


def unix_to_utc(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def bike_type_count(types: list[dict[str, int]], key: str) -> int:
    return sum(int(item.get(key, 0)) for item in types)


def normalize_status_payload(
    payload: dict[str, Any], *, snapshot_id: str, collected_at_utc: datetime
) -> NormalizedBatch:
    gbfs = GbfsPayload.model_validate(payload)
    feed_updated_at = unix_to_utc(gbfs.last_updated)
    rows: list[NormalizedStatus] = []
    errors: list[RecordError] = []
    for raw in gbfs.data["stations"]:
        station_id = str(raw.get("station_id", "unknown"))
        try:
            record = StationStatusRecord.model_validate(raw)
            last_reported = unix_to_utc(record.last_reported)
            rows.append(
                NormalizedStatus(
                    snapshot_id=snapshot_id,
                    collected_at_utc=collected_at_utc,
                    feed_updated_at_utc=feed_updated_at,
                    station_id=record.station_id,
                    station_last_reported_utc=last_reported,
                    data_age_seconds=max(
                        0, int((collected_at_utc - last_reported).total_seconds())
                    ),
                    is_installed=bool(record.is_installed),
                    is_renting=bool(record.is_renting),
                    is_returning=bool(record.is_returning),
                    available_bikes=record.num_bikes_available,
                    mechanical_bikes=bike_type_count(
                        record.num_bikes_available_types, "mechanical"
                    ),
                    electric_bikes=bike_type_count(record.num_bikes_available_types, "ebike"),
                    available_docks=record.num_docks_available,
                )
            )
        except ValidationError as exc:
            errors.append(
                RecordError(station_id=station_id, code="status_validation_error", message=str(exc))
            )
    return NormalizedBatch(kind="status", rows=rows, errors=errors)


def normalize_information_payload(payload: dict[str, Any]) -> NormalizedBatch:
    gbfs = GbfsPayload.model_validate(payload)
    rows: list[NormalizedInformation] = []
    errors: list[RecordError] = []
    for raw in gbfs.data["stations"]:
        station_id = str(raw.get("station_id", "unknown"))
        try:
            record = StationInformationRecord.model_validate(raw)
            rows.append(
                NormalizedInformation(
                    station_id=record.station_id,
                    name=record.name,
                    latitude=record.lat,
                    longitude=record.lon,
                    capacity=record.capacity,
                )
            )
        except ValidationError as exc:
            errors.append(
                RecordError(
                    station_id=station_id, code="information_validation_error", message=str(exc)
                )
            )
    return NormalizedBatch(kind="information", rows=rows, errors=errors)
