from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

CATALOG_VERSION = "1"
EARTH_RADIUS_METRES = 6_371_000


@dataclass(frozen=True)
class BusyZone:
    name: str
    latitude: float
    longitude: float
    radius_metres: int
    source_url: str


# Approximate public landmark coordinates; radii are product assumptions.
# Central Paris uses the app's existing map center.
BUSY_ZONES = (
    BusyZone("Central Paris", 48.8566, 2.3522, 1500, "https://en.wikipedia.org/wiki/Paris"),
    BusyZone("La Défense", 48.89278, 2.23583, 1500, "https://en.wikipedia.org/wiki/Grande_Arche"),
    BusyZone("Gare du Nord", 48.88111, 2.35528, 750, "https://en.wikipedia.org/wiki/Gare_du_Nord"),
    BusyZone(
        "Gare de l'Est", 48.87694, 2.35917, 750, "https://en.wikipedia.org/wiki/Gare_de_l'Est"
    ),
    BusyZone(
        "Gare Saint-Lazare",
        48.87694,
        2.32444,
        750,
        "https://en.wikipedia.org/wiki/Gare_Saint-Lazare",
    ),
    BusyZone(
        "Gare Montparnasse",
        48.84000,
        2.31861,
        750,
        "https://en.wikipedia.org/wiki/Gare_Montparnasse",
    ),
    BusyZone("Gare de Lyon", 48.8448, 2.3735, 750, "https://en.wikipedia.org/wiki/Gare_de_Lyon"),
    BusyZone(
        "Gare d'Austerlitz",
        48.84222,
        2.36583,
        750,
        "https://en.wikipedia.org/wiki/Gare_d'Austerlitz",
    ),
)


def distance_metres(latitude: float, longitude: float, zone: BusyZone) -> float:
    lat1, lat2 = radians(latitude), radians(zone.latitude)
    dlat = lat2 - lat1
    dlon = radians(zone.longitude - longitude)
    haversine = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_METRES * asin(sqrt(min(1.0, max(0.0, haversine))))


def matching_zones(
    latitude: float, longitude: float, zones: tuple[BusyZone, ...] = BUSY_ZONES
) -> tuple[str, ...]:
    return tuple(
        zone.name
        for zone in zones
        if distance_metres(latitude, longitude, zone) <= zone.radius_metres
    )
