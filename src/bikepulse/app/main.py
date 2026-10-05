from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from bikepulse.app.live_data import (
    LiveDataUnavailableError,
    LiveStationService,
    StationMapRow,
    StationMapSnapshot,
    make_live_station_service,
)
from bikepulse.app.view_model import AvailabilityMode, availability_view, map_rows
from bikepulse.config import load_settings

PARIS = ZoneInfo("Europe/Paris")
PARIS_LATITUDE = 48.8566
PARIS_LONGITUDE = 2.3522


@st.cache_resource
def _service(config_path: str) -> LiveStationService:
    return make_live_station_service(load_settings(config_path))


def _local_time(value: datetime | None) -> str:
    if value is None:
        return "Unknown"
    return value.astimezone(PARIS).strftime("%d %b %Y, %H:%M:%S %Z")


def _age_text(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "Unknown"
    if age_seconds < 60:
        return f"{age_seconds}s ago"
    return f"{age_seconds // 60}m ago"


def _status_text(value: bool | None, enabled: str, disabled: str) -> str:
    if value is None:
        return "Unknown"
    return enabled if value else disabled


def _selected_station(snapshot: StationMapSnapshot) -> StationMapRow | None:
    station_id = st.session_state.get("selected_station_id")
    return next((row for row in snapshot.stations if row.station_id == station_id), None)


def _render_header(snapshot: StationMapSnapshot) -> None:
    status_column, update_column, network_column = st.columns([1, 2, 1])
    if snapshot.is_last_known:
        status_column.error("Last known data", icon="⚠️")
    else:
        status_column.success("Live feed connected", icon="🟢")
    update_column.metric("Feed updated", _local_time(snapshot.status_source_updated_at_utc))
    network_column.metric("Stations on map", f"{len(snapshot.stations):,}")

    if snapshot.is_last_known:
        st.warning(
            "The latest refresh failed. Availability colors are disabled until the feed "
            f"recovers. Last successful fetch: {_local_time(snapshot.fetched_at_utc)}."
        )
    if snapshot.rejected_record_count or snapshot.missing_status_count:
        st.caption(
            f"Data quality: {snapshot.rejected_record_count} rejected record(s), "
            f"{snapshot.missing_status_count} station(s) without usable status, and "
            f"{snapshot.excluded_station_count} station(s) excluded from the map."
        )


def _render_controls(snapshot: StationMapSnapshot) -> tuple[AvailabilityMode, bool]:
    search_column, mode_column, refresh_column = st.columns([3, 2, 1])
    station_ids = [""] + [row.station_id for row in snapshot.stations]
    station_by_id = {row.station_id: row for row in snapshot.stations}

    def station_label(station_id: str) -> str:
        if not station_id:
            return "Search by station name or ID"
        station = station_by_id[station_id]
        return f"{station.name} · {station.station_id}"

    with search_column:
        search_choice = st.selectbox(
            "Find a station",
            station_ids,
            format_func=station_label,
            key="search_station_id",
            label_visibility="collapsed",
        )
    previous_search = st.session_state.get("previous_search_station_id", "")
    if search_choice != previous_search:
        st.session_state.selected_station_id = search_choice or None
        st.session_state.previous_search_station_id = search_choice

    with mode_column:
        mode = st.segmented_control(
            "Availability mode",
            options=["bikes", "docks"],
            format_func=lambda value: "Available bikes" if value == "bikes" else "Open docks",
            default="bikes",
            key="availability_mode",
            label_visibility="collapsed",
        )
    with refresh_column:
        refresh = st.button("Refresh data", width="stretch", type="secondary")
    selected_mode: AvailabilityMode = "docks" if mode == "docks" else "bikes"
    return selected_mode, refresh


def _render_map(
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    max_age_seconds: int,
    now_utc: datetime,
) -> None:
    selected = _selected_station(snapshot)
    latitude = selected.latitude if selected else PARIS_LATITUDE
    longitude = selected.longitude if selected else PARIS_LONGITUDE
    zoom = 15 if selected else 11.3
    data = map_rows(
        snapshot.stations,
        mode=mode,
        now_utc=now_utc,
        max_age_seconds=max_age_seconds,
        last_known=snapshot.is_last_known,
    )
    point_layer = pdk.Layer(
        "ScatterplotLayer",
        data=data,
        id="station-points",
        get_position="[longitude, latitude]",
        get_fill_color="color",
        get_line_color=[255, 255, 255, 220],
        get_radius=65,
        radius_min_pixels=5,
        radius_max_pixels=12,
        line_width_min_pixels=1,
        pickable=True,
        auto_highlight=True,
        stroked=True,
    )
    deck = pdk.Deck(
        map_style=None,
        initial_view_state=pdk.ViewState(
            latitude=latitude,
            longitude=longitude,
            zoom=zoom,
            pitch=0,
        ),
        layers=[point_layer],
        tooltip={
            "html": (
                "<b>{name}</b><br/>Availability: {availability_text}<br/>Status: {status_label}"
            ),
            "style": {"backgroundColor": "#172033", "color": "white"},
        },
    )
    event = st.pydeck_chart(
        deck,
        width="stretch",
        height=610,
        on_select="rerun",
        selection_mode="single-object",
        key="station-map",
    )
    objects = event.selection.objects.get("station-points", [])
    if objects:
        station_id = str(objects[0].get("station_id", ""))
        if station_id:
            st.session_state.selected_station_id = station_id


def _render_station_details(
    station: StationMapRow | None,
    *,
    mode: AvailabilityMode,
    max_age_seconds: int,
    now_utc: datetime,
    last_known: bool,
) -> None:
    with st.container(border=True):
        st.subheader("Station details")
        if station is None:
            st.info("Select a point on the map or search for a station.")
            return

        view = availability_view(
            station,
            mode=mode,
            now_utc=now_utc,
            max_age_seconds=max_age_seconds,
            last_known=last_known,
        )
        st.markdown(f"### {station.name}")
        st.caption(f"Station ID {station.station_id} · {view.label}")
        bikes_column, docks_column = st.columns(2)
        bikes_column.metric(
            "Available bikes",
            "—" if station.available_bikes is None else station.available_bikes,
        )
        docks_column.metric(
            "Open docks",
            "—" if station.available_docks is None else station.available_docks,
        )
        mechanical_column, electric_column = st.columns(2)
        mechanical_column.metric(
            "Mechanical",
            "—" if station.mechanical_bikes is None else station.mechanical_bikes,
        )
        electric_column.metric(
            "Electric",
            "—" if station.electric_bikes is None else station.electric_bikes,
        )
        st.divider()
        st.write(f"**Capacity:** {station.capacity}")
        st.write(
            "**Rental status:** "
            + _status_text(station.is_renting, "Rentals available", "Rentals unavailable")
        )
        st.write(
            "**Return status:** "
            + _status_text(station.is_returning, "Returns available", "Returns unavailable")
        )
        st.write(f"**Last station report:** {_local_time(station.station_last_reported_utc)}")
        st.caption(f"Freshness: {_age_text(view.age_seconds)}")


@st.fragment(run_every=60)
def _live_dashboard(config_path: str) -> None:
    service = _service(config_path)
    force_status = st.session_state.pop("force_status_refresh", False)
    try:
        snapshot = service.get_snapshot(force_status=force_status)
    except LiveDataUnavailableError as exc:
        st.error(f"Vélib live data is unavailable: {exc}")
        if st.button("Try again", type="primary"):
            st.session_state.force_status_refresh = True
            st.rerun(scope="fragment")
        return

    _render_header(snapshot)
    mode, refresh_requested = _render_controls(snapshot)
    if refresh_requested:
        st.session_state.force_status_refresh = True
        st.rerun(scope="fragment")

    now_utc = datetime.now(UTC)
    map_column, details_column = st.columns([3, 1], gap="large")
    with map_column:
        st.markdown(
            "<div class='map-legend'>"
            "<span><i class='legend-dot green'></i>4+ available</span>"
            "<span><i class='legend-dot amber'></i>1-3 available</span>"
            "<span><i class='legend-dot red'></i>None available</span>"
            "<span><i class='legend-dot grey'></i>Unavailable or stale</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        _render_map(
            snapshot,
            mode=mode,
            max_age_seconds=service.max_station_age_seconds,
            now_utc=now_utc,
        )
        st.caption("Map data: Velib Metropole open data · Base map: CARTO")
    with details_column:
        _render_station_details(
            _selected_station(snapshot),
            mode=mode,
            max_age_seconds=service.max_station_age_seconds,
            now_utc=now_utc,
            last_known=snapshot.is_last_known,
        )


def main() -> None:
    st.set_page_config(page_title="BikePulse · Live stations", page_icon="🚲", layout="wide")
    st.markdown(
        """
        <style>
        .stApp { background: #f7f9fc; color: #172033; }
        .block-container { padding-top: 1.5rem; max-width: 1500px; }
        h1 { letter-spacing: -0.035em; }
        .map-legend { display: flex; flex-wrap: wrap; gap: .7rem 1.2rem;
          margin: .2rem 0 .65rem; color: #48536a; font-size: .86rem; }
        .map-legend span { display: inline-flex; align-items: center; gap: .38rem; }
        .legend-dot { width: .68rem; height: .68rem; border-radius: 50%; display: inline-block; }
        .legend-dot.green { background: #189768; }
        .legend-dot.amber { background: #f5a623; }
        .legend-dot.red { background: #dc3545; }
        .legend-dot.grey { background: #808a96; }
        [data-testid="stMetric"] { background: white; border: 1px solid #e4e8f0;
          padding: .65rem .8rem; border-radius: .75rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("BikePulse")
    st.caption("Live Velib station availability across Greater Paris")
    config_path = os.getenv("BIKEPULSE_CONFIG", str(Path("config.example.toml")))
    _live_dashboard(config_path)


if __name__ == "__main__":
    main()
