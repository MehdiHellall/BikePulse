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
from bikepulse.prediction.rules import HORIZON, StationOutlook, forecast_station
from bikepulse.prediction.weather import WeatherService

PARIS = ZoneInfo("Europe/Paris")
PARIS_LATITUDE = 48.8566
PARIS_LONGITUDE = 2.3522


@st.cache_resource
def _service(config_path: str) -> LiveStationService:
    return make_live_station_service(load_settings(config_path))


@st.cache_resource
def _weather_service(config_path: str) -> WeatherService:
    return WeatherService(load_settings(config_path).weather)


def _local_time(value: datetime | None) -> str:
    if value is None:
        return "Unknown"
    return value.astimezone(PARIS).strftime("%d %b %Y, %H:%M:%S %Z")


def _compact_local_time(value: datetime | None) -> str:
    if value is None:
        return "Unknown"
    return value.astimezone(PARIS).strftime("%d %b · %H:%M")


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
    if snapshot.is_last_known:
        status_tone = "warning"
        status_label = "Last known data"
    else:
        status_tone = "live"
        status_label = "Live feed connected"
    source_updated_at = snapshot.status_source_updated_at_utc
    st.markdown(
        "<div class='atlas-network-bar'>"
        f"<div class='atlas-service-status {status_tone}'>"
        f"<span><i></i>{status_label}</span>"
        "<small>Automatic refresh · 60 sec</small>"
        "</div>"
        "<div class='atlas-network-stat'><span>Feed updated</span>"
        "<strong>"
        f"<span class='atlas-time-full'>{_local_time(source_updated_at)}</span>"
        f"<span class='atlas-time-compact'>{_compact_local_time(source_updated_at)}</span>"
        "</strong></div>"
        "<div class='atlas-network-stat'><span>Stations on map</span>"
        f"<strong>{len(snapshot.stations):,}</strong></div>"
        "</div>",
        unsafe_allow_html=True,
    )

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
    warning_age_seconds: int,
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
        warning_age_seconds=warning_age_seconds,
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
    selected_layer = pdk.Layer(
        "ScatterplotLayer",
        data=[row for row in data if selected and row["station_id"] == selected.station_id],
        id="selected-station",
        get_position="[longitude, latitude]",
        get_fill_color=[49, 93, 255, 28],
        get_line_color=[49, 93, 255, 255],
        get_radius=110,
        radius_min_pixels=13,
        radius_max_pixels=19,
        line_width_min_pixels=3,
        pickable=False,
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
        layers=[selected_layer, point_layer],
        tooltip={
            "html": (
                "<b>{name}</b><br/>Availability: {availability_text}<br/>Status: {status_label}"
            ),
            "style": {"backgroundColor": "#17212b", "color": "#f2efe7"},
        },
    )
    event = st.pydeck_chart(
        deck,
        width="stretch",
        height=430,
        on_select="rerun",
        selection_mode="single-object",
        key="station-map",
    )
    objects = event.selection.objects.get("station-points", [])
    if objects:
        station_id = str(objects[0].get("station_id", ""))
        if station_id:
            st.session_state.selected_station_id = station_id


def _station_outlook(
    station: StationMapRow,
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    service: LiveStationService,
    config_path: str,
) -> StationOutlook:
    outlook = forecast_station(
        station,
        snapshot,
        mode=mode,
        now_utc=now_utc,
        station_warning_age_seconds=service.station_warning_age_seconds,
        max_station_age_seconds=service.max_station_age_seconds,
        max_feed_age_seconds=service.max_feed_age_seconds,
    )
    if outlook.status != "estimated":
        return outlook
    weather = _weather_service(config_path).get_conditions(
        target_utc=now_utc + HORIZON,
        now_utc=now_utc,
    )
    return forecast_station(
        station,
        snapshot,
        mode=mode,
        now_utc=now_utc,
        weather=weather,
        station_warning_age_seconds=service.station_warning_age_seconds,
        max_station_age_seconds=service.max_station_age_seconds,
        max_feed_age_seconds=service.max_feed_age_seconds,
    )


def _render_outlook_status(outlook: StationOutlook, *, mode: AvailabilityMode) -> None:
    target = "no bikes" if mode == "bikes" else "no standard docks"
    if outlook.status == "estimated":
        label = f"{outlook.risk} risk of {target}"
        if outlook.risk == "High":
            st.error(label)
        elif outlook.risk == "Medium":
            st.warning(label)
        else:
            st.info(label)
    elif outlook.status == "already_empty":
        st.error("Already empty")
    elif outlook.status == "already_full":
        st.error("Already full")
    else:
        st.info("Prediction unavailable")


def _render_station_strip(
    station: StationMapRow | None,
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    service: LiveStationService,
    config_path: str,
) -> None:
    if station is None:
        st.info("Select a point on the map or search for a station to inspect availability.")
        return

    view = availability_view(
        station,
        mode=mode,
        now_utc=now_utc,
        max_age_seconds=service.max_station_age_seconds,
        warning_age_seconds=service.station_warning_age_seconds,
        last_known=snapshot.is_last_known,
    )
    outlook = _station_outlook(
        station,
        snapshot,
        mode=mode,
        now_utc=now_utc,
        service=service,
        config_path=config_path,
    )

    with st.container(border=True):
        st.markdown("<span class='atlas-station-anchor'></span>", unsafe_allow_html=True)
        identity_column, inventory_column, outlook_column = st.columns(
            [1.15, 1.55, 2.1], gap="large", vertical_alignment="center"
        )
        with identity_column:
            st.subheader(station.name)
            st.caption(f"Station {station.station_id} · {view.label}")
            st.markdown(f"**Reported:** {_age_text(view.age_seconds)}")
            st.caption(f"Capacity {station.capacity}")
        with inventory_column:
            st.subheader("Current availability")
            bikes_column, docks_column = st.columns(2)
            bikes_column.metric(
                "Available bikes",
                "—" if station.available_bikes is None else station.available_bikes,
            )
            docks_column.metric(
                "Open docks",
                "—" if station.available_docks is None else station.available_docks,
            )
            mechanical = "—" if station.mechanical_bikes is None else station.mechanical_bikes
            electric = "—" if station.electric_bikes is None else station.electric_bikes
            st.caption(f"{mechanical} mechanical · {electric} electric")
        with outlook_column:
            st.subheader("Next 30 minutes")
            st.caption("Rule-based estimate — not validated")
            _render_outlook_status(outlook, mode=mode)
            st.markdown(
                f"**Until {_local_time(outlook.target_utc)}** · {outlook.area} · {outlook.period}"
            )
            if outlook.status == "estimated":
                if outlook.weather is None:
                    st.caption("Weather unavailable — the station estimate still works.")
                else:
                    weather = outlook.weather
                    st.markdown(
                        f"**Paris weather:** {weather.temperature_c:g}°C · "
                        f"{weather.precipitation_probability:g}% rain · "
                        f"{weather.wind_kmh:g} km/h wind"
                    )
                    st.caption(
                        "Based on current availability, station area, time of day and weather."
                    )

    operations_column, reasons_column = st.columns([1, 2], gap="large")
    with operations_column, st.expander("Station operations"):
        st.write(
            "**Rental status:** "
            + _status_text(station.is_renting, "Rentals available", "Rentals unavailable")
        )
        st.write(
            "**Return status:** "
            + _status_text(station.is_returning, "Returns available", "Returns unavailable")
        )
        st.write(f"**Last station report:** {_local_time(station.station_last_reported_utc)}")
    with reasons_column, st.expander("Why this outlook?"):
        for reason in outlook.reasons:
            st.write(reason)
        st.caption(
            "Busy areas and peak periods use the same turnover assumption for bikes and docks. "
            "Returns, departures and rebalancing can change the outcome."
        )
        st.caption("Weather: [Open-Meteo](https://open-meteo.com/) · CC BY 4.0")


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
    st.markdown(
        "<div class='atlas-map-heading'>"
        "<div class='atlas-map-title'><span>01</span><strong>Network coverage</strong>"
        "<small>Live station inventory</small></div>"
        "<div class='map-legend'>"
        "<span><i class='legend-dot green'></i>4+ available</span>"
        "<span><i class='legend-dot amber'></i>1-3 available</span>"
        "<span><i class='legend-dot red'></i>None available</span>"
        "<span><i class='legend-dot grey'></i>Unavailable or stale</span>"
        "</div></div>",
        unsafe_allow_html=True,
    )
    _render_map(
        snapshot,
        mode=mode,
        max_age_seconds=service.max_station_age_seconds,
        warning_age_seconds=service.station_warning_age_seconds,
        now_utc=now_utc,
    )
    st.caption("Map data: Vélib Métropole open data · Basemap: CARTO")
    _render_station_strip(
        _selected_station(snapshot),
        snapshot,
        mode=mode,
        now_utc=now_utc,
        service=service,
        config_path=config_path,
    )


def main() -> None:
    st.set_page_config(page_title="BikePulse · Stations", page_icon="🚲", layout="wide")
    first_visit = not st.session_state.get("atlas_seen", False)
    st.session_state.atlas_seen = True
    entry_class = " atlas-enter" if first_visit else ""
    st.markdown(
        """
        <style>
        :root {
          --atlas-paper: #f2efe7;
          --atlas-paper-raised: #faf8f2;
          --atlas-ink: #17212b;
          --atlas-muted: #4c5861;
          --atlas-line: #c5bfb2;
          --atlas-blue: #315dff;
          --atlas-vermilion: #e5573f;
          --atlas-green: #16875f;
          --atlas-amber: #c88415;
        }
        html { background: var(--atlas-paper); }
        .stApp {
          color: var(--atlas-ink);
          background-color: var(--atlas-paper);
          background-image:
            linear-gradient(rgba(23, 33, 43, .026) 1px, transparent 1px),
            linear-gradient(90deg, rgba(23, 33, 43, .026) 1px, transparent 1px);
          background-size: 32px 32px;
        }
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        footer { display: none !important; }
        .block-container { padding: 2rem 3rem 4rem; max-width: 1500px; }
        h1, h2, h3 {
          color: var(--atlas-ink);
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia,
            serif !important;
        }
        h1 {
          font-size: clamp(4rem, 8vw, 7.4rem) !important;
          font-weight: 600 !important;
          letter-spacing: -.072em !important;
          line-height: .84 !important;
          margin: .4rem 0 .85rem !important;
          max-width: max-content;
          position: relative;
        }
        h1::after {
          background: var(--atlas-blue);
          bottom: -.18rem;
          content: '';
          height: .12em;
          position: absolute;
          right: -.32em;
          width: .32em;
        }
        h2, h3 {
          font-weight: 500;
          letter-spacing: -0.025em;
        }
        p, label, button, input, [data-baseweb="select"] {
          font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
            "Segoe UI", sans-serif;
        }
        .atlas-edition {
          align-items: center;
          display: flex;
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: .72rem;
          font-weight: 700;
          gap: .75rem;
          letter-spacing: .14em;
          text-transform: uppercase;
        }
        .atlas-edition::before {
          background: var(--atlas-blue);
          content: '';
          height: .62rem;
          width: .62rem;
        }
        .atlas-masthead {
          align-items: end;
          border-bottom: 2px solid var(--atlas-ink);
          border-top: 1px solid var(--atlas-ink);
          display: grid;
          grid-template-columns: minmax(15rem, 1fr) minmax(24rem, .9fr);
          justify-content: space-between;
          gap: 2rem;
          margin: 0 0 1.35rem;
          padding: 1rem 0 1.1rem;
        }
        .atlas-masthead-copy p {
          color: var(--atlas-ink);
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: clamp(1.05rem, 1.65vw, 1.35rem);
          line-height: 1.35;
          margin: 0 0 .4rem;
          max-width: 48ch;
        }
        .atlas-coordinates {
          color: var(--atlas-muted);
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: .72rem;
          letter-spacing: .06em;
          text-transform: uppercase;
        }
        .atlas-route {
          min-width: 0;
          padding-bottom: .15rem;
        }
        .atlas-route-line {
          background: var(--atlas-ink);
          display: block;
          height: 2px;
          margin: 0 .3rem .72rem;
          position: relative;
          transform-origin: left center;
        }
        .atlas-route-line i {
          background: var(--atlas-paper);
          border: 2px solid var(--atlas-ink);
          border-radius: 50%;
          height: .72rem;
          position: absolute;
          top: 50%;
          transform: translate(-50%, -50%);
          width: .72rem;
        }
        .atlas-route-line i:nth-child(1) { left: 0; background: var(--atlas-green); }
        .atlas-route-line i:nth-child(2) { left: 50%; background: var(--atlas-blue); }
        .atlas-route-line i:nth-child(3) { left: 100%; background: var(--atlas-paper); }
        .atlas-route-labels {
          display: grid;
          grid-template-columns: repeat(3, 1fr);
        }
        .atlas-route-labels span {
          color: var(--atlas-muted);
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: .72rem;
          letter-spacing: .07em;
          text-transform: uppercase;
        }
        .atlas-route-labels span:nth-child(2) { text-align: center; }
        .atlas-route-labels span:last-child { text-align: right; }
        .atlas-route-labels strong {
          color: var(--atlas-ink);
          display: block;
          font-family: Inter, ui-sans-serif, system-ui, sans-serif;
          font-size: .78rem;
          letter-spacing: 0;
          margin-bottom: .1rem;
        }
        .atlas-service-status,
        .atlas-network-stat {
          border-bottom: 1px solid var(--atlas-line);
          border-top: 1px solid var(--atlas-line);
          min-height: 4.25rem;
          padding: .62rem 0;
        }
        .atlas-network-bar {
          display: grid;
          gap: 2rem;
          grid-template-columns: 1fr 2fr 1fr;
        }
        .atlas-service-status > span {
          align-items: center;
          color: var(--atlas-ink);
          display: flex;
          font-size: .88rem;
          font-weight: 650;
          gap: .48rem;
          margin-bottom: .2rem;
        }
        .atlas-service-status i {
          background: var(--atlas-green);
          border-radius: 50%;
          box-shadow: 0 0 0 4px rgba(22, 135, 95, .12);
          height: .55rem;
          width: .55rem;
        }
        .atlas-service-status.warning i { background: var(--atlas-amber); }
        .atlas-service-status small,
        .atlas-network-stat span {
          display: block;
          color: var(--atlas-muted);
          font-size: .75rem;
          margin-bottom: .24rem;
        }
        .atlas-network-stat strong {
          display: block;
          color: var(--atlas-ink);
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: 1rem;
          font-weight: 650;
          font-variant-numeric: tabular-nums;
        }
        .atlas-time-compact { display: none !important; }
        .atlas-map-heading {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 1rem;
          margin: 1.65rem 0 .65rem;
        }
        .atlas-map-title { align-items: baseline; display: flex; gap: .62rem; }
        .atlas-map-title > span {
          background: var(--atlas-ink);
          color: var(--atlas-paper);
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: .72rem;
          padding: .22rem .33rem;
        }
        .atlas-map-title > strong {
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: 1.3rem;
          font-weight: 600;
        }
        .atlas-map-title > small { color: var(--atlas-muted); font-size: .72rem; }
        .map-legend { display: flex; flex-wrap: wrap; justify-content: flex-end;
          gap: .55rem 1rem; color: var(--atlas-muted); font-size: .8rem; }
        .map-legend span { display: inline-flex; align-items: center; gap: .38rem; }
        .legend-dot { width: .6rem; height: .6rem; border-radius: 50%; display: inline-block; }
        .legend-dot.green { background: #16875f; }
        .legend-dot.amber { background: #c88415; }
        .legend-dot.red { background: #c5443a; }
        .legend-dot.grey { background: #6f7a83; }
        [data-testid="stMetric"] { background: transparent; border: 0; padding: .1rem 0; }
        [data-testid="stMetricLabel"] { color: var(--atlas-muted); }
        [data-testid="stMetricValue"] {
          color: var(--atlas-ink);
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: 3rem;
          font-weight: 600;
          font-variant-numeric: tabular-nums;
          letter-spacing: -.04em;
        }
        [data-testid="stVerticalBlock"]:has(
          > [data-testid="stElementContainer"] .atlas-station-anchor
        ) {
          background: rgba(250, 248, 242, .96);
          border: 1px solid var(--atlas-ink);
          border-radius: 0;
          box-shadow: 6px 6px 0 rgba(49, 93, 255, .14);
          margin-top: .9rem;
          overflow: hidden;
          position: relative;
          animation: atlas-strip-in 280ms cubic-bezier(.22, .8, .35, 1) both;
        }
        [data-testid="stVerticalBlock"]:has(
          > [data-testid="stElementContainer"] .atlas-station-anchor
        )::before {
          background: var(--atlas-blue);
          content: '';
          height: 5px;
          left: 0;
          position: absolute;
          right: 0;
          top: 0;
          transform-origin: left center;
          animation: atlas-line-in 420ms 80ms cubic-bezier(.22, .8, .35, 1) both;
        }
        [data-testid="stDeckGlJsonChart"] {
          overflow: hidden;
          border: 2px solid var(--atlas-ink);
          border-radius: 0;
          background: #ebe8df;
          box-shadow: 7px 7px 0 rgba(49, 93, 255, .2);
        }
        [data-testid="stAlert"] { border-radius: 0; box-shadow: none; }
        [data-testid="stAlert"] p { color: var(--atlas-ink) !important; }
        [data-testid="stExpander"] {
          background: rgba(250, 248, 242, .5);
          border-color: var(--atlas-line);
          border-radius: 0;
        }
        [data-testid="stExpander"] p { max-width: 72ch; }
        div[data-baseweb="select"] > div,
        [data-testid="stButton"] button,
        [data-testid="stButtonGroup"] button {
          border-radius: 0;
          min-height: 2.55rem;
          transition: transform 140ms ease, border-color 180ms ease, background-color 180ms ease,
            box-shadow 180ms ease;
        }
        [data-testid="stButton"] button:hover,
        [data-testid="stButtonGroup"] button:hover {
          border-color: var(--atlas-blue);
          box-shadow: 3px 3px 0 rgba(49, 93, 255, .14);
        }
        div[data-baseweb="select"] > div { border-color: var(--atlas-line) !important; }
        div[data-baseweb="select"]:focus-within > div {
          border-color: var(--atlas-blue) !important;
          box-shadow: 3px 3px 0 rgba(49, 93, 255, .14);
        }
        [data-testid="stButtonGroup"] p { color: var(--atlas-ink) !important; }
        [data-testid="stButtonGroup"] button[aria-checked="true"],
        [data-testid="stButtonGroup"] button[aria-pressed="true"] {
          background: var(--atlas-blue) !important;
          border-color: var(--atlas-blue) !important;
          color: white !important;
        }
        [data-testid="stButtonGroup"] button[aria-checked="true"] p,
        [data-testid="stButtonGroup"] button[aria-pressed="true"] p {
          color: white !important;
        }
        [data-testid="stButton"] button:active,
        [data-testid="stButtonGroup"] button:active { transform: translate(2px, 2px); }
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] p {
          color: var(--atlas-muted) !important;
          opacity: 1;
        }
        a { color: var(--atlas-blue); }
        a[href="#stMain"] { color: var(--atlas-ink) !important; background: white !important; }
        .atlas-enter { animation: atlas-enter 460ms cubic-bezier(.22, .8, .35, 1) both; }
        .stApp:has(.atlas-enter) h1 {
          animation: atlas-title-in 520ms cubic-bezier(.22, .8, .35, 1) both;
        }
        .stApp:has(.atlas-enter) .atlas-route-line {
          animation: atlas-route-in 620ms 180ms cubic-bezier(.22, .8, .35, 1) both;
        }
        @keyframes atlas-title-in {
          from { clip-path: inset(0 100% 0 0); transform: translateY(10px); }
          to { clip-path: inset(0 -20% 0 0); transform: translateY(0); }
        }
        @keyframes atlas-route-in {
          from { transform: scaleX(0); }
          to { transform: scaleX(1); }
        }
        @keyframes atlas-enter {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @keyframes atlas-strip-in {
          from { opacity: 0; transform: translateY(10px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @keyframes atlas-line-in {
          from { transform: scaleX(0); }
          to { transform: scaleX(1); }
        }
        @media (prefers-reduced-motion: reduce) {
          *, *::before, *::after { animation: none !important; transition: none !important; }
        }
        @media (max-width: 900px) {
          .block-container { padding: 1.15rem 1rem 3rem; }
          h1 { font-size: clamp(3.5rem, 17vw, 5.6rem) !important; }
          .atlas-masthead { grid-template-columns: 1fr; gap: .95rem; padding: .8rem 0; }
          .atlas-network-bar {
            gap: .55rem;
            grid-template-columns: 1.15fr 1.3fr .55fr;
          }
          .atlas-service-status,
          .atlas-network-stat { min-height: 4.7rem; }
          .atlas-service-status > span { align-items: flex-start; font-size: .8rem; }
          .atlas-service-status small,
          .atlas-network-stat span { font-size: .72rem; line-height: 1.3; }
          .atlas-network-stat strong { font-size: .84rem; line-height: 1.35; }
          .atlas-time-full { display: none !important; }
          .atlas-time-compact { display: block !important; }
          [data-testid="stHorizontalBlock"]:has([data-testid="stSelectbox"]) {
            display: grid;
            gap: .65rem;
            grid-template-columns: 1.5fr .8fr;
          }
          [data-testid="stHorizontalBlock"]:has([data-testid="stSelectbox"])
          > [data-testid="stColumn"]:first-child { grid-column: 1 / -1; }
          [data-testid="stHorizontalBlock"]:has([data-testid="stSelectbox"])
          > [data-testid="stColumn"] { min-width: 0; width: auto !important; }
          [data-testid="stButtonGroup"] > div { width: 100%; }
          [data-testid="stButton"] button { width: 100%; }
          .atlas-map-heading { align-items: flex-start; flex-direction: column; }
          .atlas-map-title { flex-wrap: wrap; }
          .map-legend { justify-content: flex-start; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div class='atlas-edition'>Service map / Paris Métropole / Live data</div>",
        unsafe_allow_html=True,
    )
    st.title("BikePulse")
    config_path = os.getenv("BIKEPULSE_CONFIG", str(Path("config.example.toml")))
    st.markdown(
        f"<div class='atlas-masthead{entry_class}'>"
        "<div class='atlas-masthead-copy'>"
        "<p>Live Vélib availability, with a practical 30-minute outlook for your next stop.</p>"
        "<span class='atlas-coordinates'>48.8566° N / 2.3522° E · Europe/Paris</span>"
        "</div>"
        "<div class='atlas-route' aria-label='Decision path from live availability to outlook'>"
        "<span class='atlas-route-line'><i></i><i></i><i></i></span>"
        "<div class='atlas-route-labels'>"
        "<span><strong>Live feed</strong>Network now</span>"
        "<span><strong>Station</strong>Bikes / docks</span>"
        "<span><strong>Outlook</strong>+30 minutes</span>"
        "</div></div>"
        "</div>",
        unsafe_allow_html=True,
    )
    _live_dashboard(config_path)


if __name__ == "__main__":
    main()
