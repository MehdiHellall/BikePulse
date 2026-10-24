from __future__ import annotations

import html
import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from bikepulse.app.alternatives import (
    MAX_PLAN_B_DISTANCE_METERS,
    PlanBRecommendation,
    connector_points,
    distance_meters,
    find_plan_b,
)
from bikepulse.app.live_data import (
    LiveDataUnavailableError,
    LiveStationService,
    StationMapRow,
    StationMapSnapshot,
    make_live_station_service,
)
from bikepulse.app.outlook_presentation import present_outlook
from bikepulse.app.view_model import (
    AvailabilityMode,
    availability_view,
    map_rows,
    station_age_seconds,
)
from bikepulse.config import load_settings
from bikepulse.prediction.movement import MovementAnalysis, analyze_inventory_movement
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


def _render_header(
    snapshot: StationMapSnapshot | None, *, animate_arrival: bool = False
) -> tuple[AvailabilityMode, bool] | None:
    if snapshot is None:
        status_tone = "unavailable"
        status_label = "Feed unavailable"
        source_updated_at = None
        station_count = "—"
    elif snapshot.is_last_known:
        status_tone = "warning"
        status_label = "Last known"
        source_updated_at = snapshot.status_source_updated_at_utc
        station_count = f"{len(snapshot.stations):,}"
    else:
        status_tone = "live"
        status_label = "Live"
        source_updated_at = snapshot.status_source_updated_at_utc
        station_count = f"{len(snapshot.stations):,}"
    arrival_class = " atlas-arrival" if animate_arrival else ""
    with st.container():
        st.markdown(
            f"<span class='atlas-header-anchor{arrival_class}'></span>",
            unsafe_allow_html=True,
        )
        brand_column, service_column = st.columns([1.45, 1], vertical_alignment="center")
        with brand_column:
            st.title("BikePulse")
        with service_column:
            st.markdown(
                "<div class='atlas-service-summary'>"
                f"<span class='atlas-service-status {status_tone}'><i></i>"
                f"<strong>{status_label}</strong></span>"
                "<span class='atlas-service-detail'><b>Updated</b>"
                f"<span class='atlas-time-full'>{_local_time(source_updated_at)}</span>"
                f"<span class='atlas-time-compact'>{_compact_local_time(source_updated_at)}</span>"
                "</span>"
                "<span class='atlas-service-detail'><b>Stations</b>"
                f"{station_count}</span>"
                "</div>",
                unsafe_allow_html=True,
            )
        if snapshot is None:
            return None
        controls = _render_controls(snapshot)
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
        return controls


def _render_controls(snapshot: StationMapSnapshot) -> tuple[AvailabilityMode, bool]:
    search_column, mode_column, refresh_column = st.columns(
        [4.6, 1.4, 1.3], vertical_alignment="bottom"
    )
    station_ids = [""] + [row.station_id for row in snapshot.stations]
    station_by_id = {row.station_id: row for row in snapshot.stations}

    def station_label(station_id: str) -> str:
        if not station_id:
            return "Station name or ID"
        station = station_by_id[station_id]
        return f"{station.name} · {station.station_id}"

    with search_column:
        st.markdown(
            "<p class='atlas-task-line'><strong>Find a Vélib.</strong></p>",
            unsafe_allow_html=True,
        )
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
            format_func=lambda value: "Bikes" if value == "bikes" else "Docks",
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
    plan_b: PlanBRecommendation | None,
    max_age_seconds: int,
    warning_age_seconds: int,
    now_utc: datetime,
) -> None:
    selected = _selected_station(snapshot)
    if selected is not None and plan_b is not None:
        latitude = (selected.latitude + plan_b.station.latitude) / 2
        longitude = (selected.longitude + plan_b.station.longitude) / 2
        zoom = (
            15
            if plan_b.distance_meters < 300
            else 14.3
            if plan_b.distance_meters < 700
            else 13.5
            if plan_b.distance_meters < 1_500
            else 13
        )
    else:
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
        get_fill_color=[23, 33, 43, 24],
        get_line_color=[23, 33, 43, 255],
        get_radius=110,
        radius_min_pixels=13,
        radius_max_pixels=19,
        line_width_min_pixels=3,
        pickable=False,
        stroked=True,
    )
    plan_b_connector = pdk.Layer(
        "ScatterplotLayer",
        data=(
            connector_points(selected, plan_b.station)
            if selected is not None and plan_b is not None
            else []
        ),
        id="plan-b-connector",
        get_position="[longitude, latitude]",
        get_fill_color=[49, 93, 255, 220],
        get_radius=18,
        radius_min_pixels=2,
        radius_max_pixels=4,
        pickable=False,
    )
    plan_b_station = pdk.Layer(
        "ScatterplotLayer",
        data=(
            [
                {
                    "longitude": plan_b.station.longitude,
                    "latitude": plan_b.station.latitude,
                }
            ]
            if plan_b is not None
            else []
        ),
        id="plan-b-station",
        get_position="[longitude, latitude]",
        get_fill_color=[49, 93, 255, 35],
        get_line_color=[49, 93, 255, 255],
        get_radius=95,
        radius_min_pixels=12,
        radius_max_pixels=18,
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
        layers=[plan_b_connector, point_layer, plan_b_station, selected_layer],
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
        if station_id and station_id != st.session_state.get("selected_station_id"):
            st.session_state.selected_station_id = station_id
            st.rerun(scope="fragment")


def _station_outlook(
    station: StationMapRow,
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    service: LiveStationService,
    config_path: str,
) -> StationOutlook:
    eligibility = forecast_station(
        station,
        snapshot,
        mode=mode,
        now_utc=now_utc,
        station_warning_age_seconds=service.station_warning_age_seconds,
        max_station_age_seconds=service.max_station_age_seconds,
        max_feed_age_seconds=service.max_feed_age_seconds,
    )
    if eligibility.status != "estimated":
        return eligibility
    movement_analysis = _station_movement(station, mode=mode, now_utc=now_utc, service=service)
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
        movement_analysis=movement_analysis,
        station_warning_age_seconds=service.station_warning_age_seconds,
        max_station_age_seconds=service.max_station_age_seconds,
        max_feed_age_seconds=service.max_feed_age_seconds,
    )


def _station_movement(
    station: StationMapRow,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    service: LiveStationService,
) -> MovementAnalysis | None:
    count = station.available_bikes if mode == "bikes" else station.available_docks
    if count is None:
        return None
    return analyze_inventory_movement(
        service.station_observations(station.station_id, now_utc=now_utc),
        mode=mode,
        current_count=count,
        capacity=station.capacity,
        now_utc=now_utc,
        settings=service.movement_settings,
    )


def _plan_b_recommendation(
    station: StationMapRow,
    outlook: StationOutlook,
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    service: LiveStationService,
) -> PlanBRecommendation | None:
    nearby_outlooks: list[tuple[StationMapRow, StationOutlook]] = []
    for candidate in snapshot.stations:
        if (
            candidate.station_id == station.station_id
            or distance_meters(station, candidate) > MAX_PLAN_B_DISTANCE_METERS
        ):
            continue
        candidate_outlook = forecast_station(
            candidate,
            snapshot,
            mode=mode,
            now_utc=now_utc,
            weather=outlook.weather,
            movement_analysis=_station_movement(
                candidate,
                mode=mode,
                now_utc=now_utc,
                service=service,
            ),
            station_warning_age_seconds=service.station_warning_age_seconds,
            max_station_age_seconds=service.max_station_age_seconds,
            max_feed_age_seconds=service.max_feed_age_seconds,
        )
        nearby_outlooks.append((candidate, candidate_outlook))
    return find_plan_b(station, outlook, nearby_outlooks, mode=mode)


def _select_plan_b(station_id: str) -> None:
    st.session_state.selected_station_id = station_id
    st.session_state.search_station_id = station_id
    st.session_state.previous_search_station_id = station_id


def _render_station_strip(
    station: StationMapRow | None,
    outlook: StationOutlook | None,
    plan_b: PlanBRecommendation | None,
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    service: LiveStationService,
) -> None:
    if station is None:
        st.info("Select a station to see availability and the 30-minute outlook.")
        return
    assert outlook is not None

    view = availability_view(
        station,
        mode=mode,
        now_utc=now_utc,
        max_age_seconds=service.max_station_age_seconds,
        warning_age_seconds=service.station_warning_age_seconds,
        last_known=snapshot.is_last_known,
    )
    presentation = present_outlook(
        outlook,
        mode=mode,
        report_age_seconds=view.age_seconds,
        warning_age_seconds=service.station_warning_age_seconds,
    )
    selected_count = station.available_bikes if mode == "bikes" else station.available_docks
    secondary_count = station.available_docks if mode == "bikes" else station.available_bikes
    selected_unit = "bikes" if mode == "bikes" else "open docks"
    secondary_label = "Open docks" if mode == "bikes" else "Available bikes"
    selected_value = "—" if selected_count is None else str(selected_count)
    secondary_value = "—" if secondary_count is None else str(secondary_count)
    if mode == "bikes":
        mechanical = "—" if station.mechanical_bikes is None else station.mechanical_bikes
        electric = "—" if station.electric_bikes is None else station.electric_bikes
        inventory_detail = f"{mechanical} mechanical · {electric} electric"
    else:
        inventory_detail = "Standard spaces for returns"
    factors = "".join(f"<span>{html.escape(factor)}</span>" for factor in presentation.factors)
    rule_note = (
        "Rule estimate · not validated"
        if outlook.status == "estimated"
        else html.escape(presentation.secondary)
    )
    decision_label = (
        f"Now: {selected_value} {selected_unit}. In 30 minutes: {presentation.headline}."
    )
    st.markdown(
        f"<section class='atlas-station-panel atlas-tone-{presentation.tone}'>"
        "<span class='atlas-station-anchor'></span>"
        "<header class='atlas-station-head'>"
        "<div><h2>"
        f"{html.escape(station.name)}"
        "</h2><p>Station "
        f"{html.escape(station.station_id)} · {html.escape(view.label)} · "
        f"{_age_text(view.age_seconds)}</p></div>"
        "<div class='atlas-secondary-stock'><span>"
        f"{secondary_label}</span><strong>{secondary_value}</strong></div>"
        "</header>"
        f"<div class='atlas-decision' aria-label='{html.escape(decision_label)}'>"
        "<div class='atlas-decision-now'><span>Now</span><div><strong>"
        f"{selected_value}</strong><em>{selected_unit}</em></div><small>"
        f"{inventory_detail}</small></div>"
        "<div class='atlas-decision-track' aria-hidden='true'><i></i><b></b></div>"
        "<div class='atlas-decision-outlook'><span>+30 min</span><strong>"
        f"{html.escape(presentation.headline)}</strong><small>{rule_note}</small></div>"
        "</div>"
        f"<div class='atlas-factors'>{factors}</div>"
        "</section>",
        unsafe_allow_html=True,
    )

    if plan_b is not None:
        plan_b_station = plan_b.station
        plan_b_inventory = (
            plan_b_station.available_bikes if mode == "bikes" else plan_b_station.available_docks
        )
        plan_b_presentation = present_outlook(
            plan_b.outlook,
            mode=mode,
            report_age_seconds=station_age_seconds(plan_b_station, now_utc),
            warning_age_seconds=service.station_warning_age_seconds,
        )
        plan_b_unit = "bikes" if mode == "bikes" else "open docks"
        with st.container(key="plan_b"):
            details_column, action_column = st.columns(
                [5, 1.35], vertical_alignment="center", gap="large"
            )
            with details_column:
                st.markdown(
                    "<div class='atlas-plan-b-copy'>"
                    "<span>Plan B</span><div><strong>"
                    f"{html.escape(plan_b_station.name)}</strong><small>"
                    f"{plan_b.distance_meters:,} m straight-line · "
                    f"{plan_b_inventory} {plan_b_unit}</small></div>"
                    "<div class='atlas-plan-b-outlook'><small>+30 min</small><strong>"
                    f"{html.escape(plan_b_presentation.headline)}</strong></div></div>",
                    unsafe_allow_html=True,
                )
            with action_column:
                st.button(
                    "Choose this station",
                    key=f"choose-plan-b-{plan_b_station.station_id}",
                    width="stretch",
                    type="primary",
                    on_click=_select_plan_b,
                    args=(plan_b_station.station_id,),
                )


def _render_station_details(station: StationMapRow, outlook: StationOutlook) -> None:
    operations_column, reasons_column = st.columns([1, 2], gap="large")
    with operations_column, st.expander("Station status"):
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
    with reasons_column, st.expander("How this is estimated"):
        st.write("**Rule-based estimate — not validated.**")
        st.write(f"**Valid until:** {_local_time(outlook.target_utc)}")
        for reason in outlook.reasons:
            st.write(reason)
        if outlook.weather is not None:
            weather = outlook.weather
            st.write(
                f"**Paris weather:** {weather.temperature_c:g}°C · "
                f"{weather.precipitation_probability:g}% rain · "
                f"{weather.wind_kmh:g} km/h wind"
            )
        else:
            st.write("**Weather:** Not included in this estimate.")
        st.caption(
            "Busy areas and peak periods use the same turnover assumption for bikes and docks. "
            "Returns, departures and rebalancing can change the outcome."
        )
        st.caption(
            f"Rules version {outlook.rules_version} · Weather: "
            "[Open-Meteo](https://open-meteo.com/) · CC BY 4.0"
        )


@st.fragment(run_every=60)
def _live_dashboard(config_path: str) -> None:
    service = _service(config_path)
    force_status = st.session_state.pop("force_status_refresh", False)
    try:
        snapshot = service.get_snapshot(force_status=force_status)
    except LiveDataUnavailableError as exc:
        _render_header(None)
        st.error(f"Vélib live data is unavailable: {exc}")
        if st.button("Try again", type="primary"):
            st.session_state.force_status_refresh = True
            st.rerun(scope="fragment")
        return

    first_success = not st.session_state.get("atlas_feed_seen", False)
    animate_arrival = not snapshot.is_last_known and (first_success or force_status)
    controls = _render_header(snapshot, animate_arrival=animate_arrival)
    assert controls is not None
    if not snapshot.is_last_known:
        st.session_state.atlas_feed_seen = True
    mode, refresh_requested = controls
    if refresh_requested:
        st.session_state.force_status_refresh = True
        st.rerun(scope="fragment")

    now_utc = datetime.now(UTC)
    selected_station = _selected_station(snapshot)
    selected_outlook = (
        _station_outlook(
            selected_station,
            snapshot,
            mode=mode,
            now_utc=now_utc,
            service=service,
            config_path=config_path,
        )
        if selected_station is not None
        else None
    )
    plan_b = (
        _plan_b_recommendation(
            selected_station,
            selected_outlook,
            snapshot,
            mode=mode,
            now_utc=now_utc,
            service=service,
        )
        if selected_station is not None and selected_outlook is not None
        else None
    )
    if selected_station is not None and selected_outlook is not None:
        _render_station_strip(
            selected_station,
            selected_outlook,
            plan_b,
            snapshot,
            mode=mode,
            now_utc=now_utc,
            service=service,
        )
    map_heading_class = (
        "atlas-map-heading atlas-map-heading-selected"
        if selected_station is not None
        else "atlas-map-heading"
    )
    st.markdown(
        f"<div class='{map_heading_class}'>"
        "<div class='atlas-map-title'><strong>Stations</strong></div>"
        "<div class='map-legend'>"
        "<span><i class='legend-dot green'></i>4+ available</span>"
        "<span><i class='legend-dot amber'></i>1-3 available</span>"
        "<span><i class='legend-dot red'></i>None available</span>"
        "<span><i class='legend-dot grey'></i>Unavailable or stale</span>"
        + ("<span><i class='legend-dot blue-ring'></i>Plan B</span>" if plan_b is not None else "")
        + "</div></div>",
        unsafe_allow_html=True,
    )
    _render_map(
        snapshot,
        mode=mode,
        plan_b=plan_b,
        max_age_seconds=service.max_station_age_seconds,
        warning_age_seconds=service.station_warning_age_seconds,
        now_utc=now_utc,
    )
    st.caption("Map data: Vélib Métropole open data · Basemap: CARTO")
    if selected_station is not None and selected_outlook is not None:
        _render_station_details(selected_station, selected_outlook)
    else:
        _render_station_strip(
            None,
            None,
            None,
            snapshot,
            mode=mode,
            now_utc=now_utc,
            service=service,
        )


def main() -> None:
    st.set_page_config(page_title="BikePulse · Stations", page_icon="🚲", layout="wide")
    st.markdown(
        """
        <style>
        :root {
          --atlas-paper: #f2efe7;
          --atlas-paper-raised: #fbf9f3;
          --atlas-ink: #17212b;
          --atlas-muted: #4c5861;
          --atlas-line: #c5bfb2;
          --atlas-blue: #315dff;
          --atlas-blue-ink: #2446c7;
          --atlas-vermilion: #c5443a;
          --atlas-green: #16875f;
          --atlas-amber: #c88415;
        }
        html { background: var(--atlas-paper); }
        .stApp {
          color: var(--atlas-ink);
          background-color: var(--atlas-paper);
        }
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        footer { display: none !important; }
        .block-container {
          max-width: 1380px;
          padding: 2rem clamp(2.25rem, 4vw, 4rem) 5rem;
        }
        h1, h2, h3 {
          color: var(--atlas-ink);
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia,
            serif !important;
        }
        h1 {
          font-size: clamp(3.5rem, 5.5vw, 5.1rem) !important;
          font-weight: 600 !important;
          letter-spacing: -.065em !important;
          line-height: .9 !important;
          margin: .1rem 0 .35rem !important;
          max-width: max-content;
          position: relative;
        }
        h2, h3 {
          font-weight: 600;
          letter-spacing: -0.025em;
        }
        p, label, button, input, [data-baseweb="select"] {
          font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
            "Segoe UI", sans-serif;
        }
        [data-testid="stVerticalBlock"]:has(
          > [data-testid="stElementContainer"] .atlas-header-anchor
        ) {
          background-color: rgba(250, 248, 242, .76);
          background-image:
            linear-gradient(rgba(23, 33, 43, .025) 1px, transparent 1px),
            linear-gradient(90deg, rgba(23, 33, 43, .025) 1px, transparent 1px);
          background-size: 32px 32px;
          border-bottom: 2px solid var(--atlas-ink);
          border-top: 1px solid var(--atlas-ink);
          margin-bottom: 1.8rem;
          overflow: hidden;
          padding: 1rem 1.35rem 1.25rem;
          position: relative;
        }
        [data-testid="stVerticalBlock"]:has(
          > [data-testid="stElementContainer"] .atlas-header-anchor.atlas-arrival
        )::after {
          animation: atlas-data-arrival 420ms cubic-bezier(.2, .75, .25, 1) both;
          background: var(--atlas-blue);
          bottom: -2px;
          content: '';
          height: 4px;
          left: 0;
          position: absolute;
          right: 0;
          transform-origin: left center;
        }
        .atlas-service-summary {
          align-items: center;
          display: grid;
          gap: 1.5rem;
          grid-template-columns: 1fr 1.75fr .6fr;
        }
        .atlas-service-status {
          align-items: center;
          display: flex;
          font-size: .88rem;
          gap: .55rem;
        }
        .atlas-service-status i {
          background: var(--atlas-green);
          border-radius: 50%;
          box-shadow: 0 0 0 4px rgba(22, 135, 95, .12);
          display: block;
          height: .55rem;
          width: .55rem;
        }
        .atlas-service-status.warning i { background: var(--atlas-amber); }
        .atlas-service-status.unavailable i { background: var(--atlas-vermilion); }
        .atlas-service-detail {
          color: var(--atlas-ink);
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: .82rem;
          font-variant-numeric: tabular-nums;
        }
        .atlas-service-detail b {
          color: var(--atlas-muted);
          display: block;
          font-family: Inter, ui-sans-serif, system-ui, sans-serif;
          font-size: .75rem;
          font-weight: 500;
          margin-bottom: .18rem;
        }
        .atlas-time-compact { display: none !important; }
        .atlas-task-line {
          color: var(--atlas-ink);
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: 1rem;
          line-height: 1.25;
          margin: 0;
          padding-bottom: .35rem;
        }
        [data-testid="stColumn"]:has(.atlas-task-line) [data-testid="stVerticalBlock"] {
          gap: .45rem;
        }
        .atlas-task-line strong,
        .atlas-task-line span { display: block; }
        .atlas-task-line span { color: var(--atlas-muted); }
        .atlas-map-heading {
          align-items: center;
          display: flex;
          gap: 1rem;
          justify-content: space-between;
          margin: .85rem 0 .75rem;
        }
        .atlas-map-heading-selected { margin-top: 2.2rem; }
        .atlas-map-title > strong {
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: 1.35rem;
          font-weight: 600;
        }
        .map-legend {
          color: var(--atlas-muted);
          display: flex;
          flex-wrap: wrap;
          font-size: .82rem;
          gap: .55rem 1rem;
          justify-content: flex-end;
        }
        .map-legend span {
          display: flex;
          align-items: center;
          gap: .38rem;
        }
        .legend-dot {
          border-radius: 50%;
          display: inline-block;
          height: .6rem;
          width: .6rem;
        }
        .legend-dot.green { background: var(--atlas-green); }
        .legend-dot.amber { background: var(--atlas-amber); }
        .legend-dot.red { background: var(--atlas-vermilion); }
        .legend-dot.grey { background: #6f7a83; }
        .legend-dot.blue-ring {
          background: var(--atlas-paper);
          border: 2px solid var(--atlas-blue);
        }
        [data-testid="stDeckGlJsonChart"] {
          background: #ebe8df;
          border: 1px solid var(--atlas-ink);
          border-radius: 0;
          overflow: hidden;
        }
        .atlas-station-panel {
          --outlook-color: var(--atlas-muted);
          animation: atlas-panel-in 280ms cubic-bezier(.22, .8, .35, 1) both;
          background: var(--atlas-paper-raised);
          border: 1px solid var(--atlas-ink);
          border-top: 4px solid var(--atlas-blue);
          margin-top: .25rem;
          padding: 1.6rem 1.75rem 1.35rem;
        }
        .atlas-tone-positive { --outlook-color: var(--atlas-green); }
        .atlas-tone-caution { --outlook-color: var(--atlas-amber); }
        .atlas-tone-critical { --outlook-color: var(--atlas-vermilion); }
        .atlas-station-anchor { display: none; }
        .atlas-station-head {
          align-items: end;
          border-bottom: 1px solid var(--atlas-line);
          display: grid;
          gap: 1rem;
          grid-template-columns: 1fr auto;
          padding-bottom: 1.15rem;
        }
        .atlas-station-head h2 {
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: clamp(1.7rem, 2.5vw, 2.35rem);
          font-weight: 600;
          letter-spacing: -.035em;
          line-height: 1.05;
          margin: 0 0 .28rem;
        }
        .atlas-station-head p {
          color: var(--atlas-muted);
          font-size: .875rem;
          margin: 0;
        }
        .atlas-secondary-stock {
          align-items: baseline;
          display: flex;
          gap: .6rem;
        }
        .atlas-secondary-stock span {
          color: var(--atlas-muted);
          font-size: .82rem;
        }
        .atlas-secondary-stock strong {
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: 1.8rem;
          font-variant-numeric: tabular-nums;
        }
        .atlas-decision {
          align-items: center;
          display: grid;
          gap: 2rem;
          grid-template-columns: minmax(11rem, auto) 1fr minmax(18rem, auto);
          padding: 1.65rem 0 1.45rem;
        }
        .atlas-decision-now > span,
        .atlas-decision-outlook > span {
          color: var(--atlas-muted);
          display: block;
          font-size: .72rem;
          font-weight: 700;
          letter-spacing: .09em;
          margin-bottom: .25rem;
          text-transform: uppercase;
        }
        .atlas-decision-now > div {
          align-items: baseline;
          display: flex;
          gap: .55rem;
        }
        .atlas-decision-now strong {
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: clamp(3.25rem, 5vw, 4.7rem);
          font-variant-numeric: tabular-nums;
          letter-spacing: -.055em;
          line-height: .9;
        }
        .atlas-decision-now em {
          font-size: 1rem;
          font-style: normal;
          font-weight: 650;
        }
        .atlas-decision-now small,
        .atlas-decision-outlook small {
          color: var(--atlas-muted);
          display: block;
          font-size: .8rem;
          margin-top: .45rem;
        }
        .atlas-decision-outlook small b {
          color: var(--atlas-line);
          margin: 0 .38rem;
        }
        .atlas-decision-outlook > strong {
          color: var(--atlas-ink);
          display: block;
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: clamp(2rem, 3.3vw, 3.15rem);
          letter-spacing: -.04em;
          line-height: 1;
        }
        .atlas-decision-track {
          animation: atlas-track-in 320ms 70ms cubic-bezier(.22, .8, .35, 1) both;
          background: var(--atlas-ink);
          height: 2px;
          position: relative;
          transform-origin: left center;
        }
        .atlas-decision-track i,
        .atlas-decision-track b {
          border: 2px solid var(--atlas-ink);
          border-radius: 50%;
          height: .8rem;
          position: absolute;
          top: 50%;
          transform: translate(-50%, -50%);
          width: .8rem;
        }
        .atlas-decision-track i {
          background: var(--atlas-ink);
          left: 0;
        }
        .atlas-decision-track b {
          animation: atlas-endpoint-in 220ms 300ms cubic-bezier(.2, .75, .25, 1) both;
          background: var(--outlook-color);
          border-color: var(--outlook-color);
          left: 100%;
        }
        .atlas-decision-track span {
          background: var(--atlas-paper-raised);
          color: var(--atlas-muted);
          font-size: .68rem;
          left: 50%;
          padding: 0 .45rem;
          position: absolute;
          top: 50%;
          transform: translate(-50%, -50%);
          white-space: nowrap;
        }
        .atlas-factors {
          border-top: 1px solid var(--atlas-line);
          color: var(--atlas-muted);
          display: flex;
          flex-wrap: wrap;
          font-size: .85rem;
          gap: .35rem;
          padding-top: 1rem;
        }
        .atlas-factors:empty { display: none; }
        .atlas-factors span + span::before {
          color: var(--atlas-line);
          content: '·';
          margin-right: .35rem;
        }
        [class*="st-key-plan_b"] {
          animation: atlas-plan-b-in 260ms cubic-bezier(.22, .8, .35, 1) both;
          background: #e8ecff;
          border: 1px solid var(--atlas-ink);
          margin-top: -1rem;
          padding: 1rem 1.25rem;
          position: relative;
        }
        [class*="st-key-plan_b"]::before {
          background: var(--atlas-blue);
          content: '';
          height: 3px;
          left: -1px;
          position: absolute;
          top: -1px;
          width: 3rem;
        }
        [class*="st-key-plan_b"] [data-testid="stHorizontalBlock"] { gap: 1.4rem; }
        .atlas-plan-b-copy {
          align-items: center;
          display: grid;
          gap: 1.2rem;
          grid-template-columns: auto 1fr auto;
        }
        .atlas-plan-b-copy > span {
          color: var(--atlas-blue-ink);
          font-size: .72rem;
          font-weight: 800;
          letter-spacing: .1em;
          text-transform: uppercase;
        }
        .atlas-plan-b-copy strong,
        .atlas-plan-b-copy small { display: block; }
        .atlas-plan-b-copy > div > strong {
          font-family: 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
          font-size: 1.18rem;
          line-height: 1.1;
        }
        .atlas-plan-b-copy small {
          color: var(--atlas-muted);
          font-size: .8rem;
          margin-top: .18rem;
        }
        .atlas-plan-b-outlook {
          border-left: 1px solid rgba(49, 93, 255, .3);
          min-width: 9.5rem;
          padding-left: 1rem;
        }
        .atlas-plan-b-outlook strong {
          color: var(--atlas-blue-ink);
          font-size: 1rem;
        }
        [class*="st-key-plan_b"] [data-testid="stButton"] button {
          background: var(--atlas-blue) !important;
          border-color: var(--atlas-blue) !important;
          color: white !important;
        }
        [class*="st-key-plan_b"] [data-testid="stButton"] button p {
          color: white !important;
        }
        [data-testid="stAlert"] { border-radius: 0; box-shadow: none; }
        [data-testid="stAlert"] p { color: var(--atlas-ink) !important; }
        [data-testid="stExpander"] {
          background: transparent;
          border-color: var(--atlas-line);
          border-radius: 0;
        }
        [data-testid="stExpander"] p { max-width: 74ch; }
        div[data-baseweb="select"] > div,
        [data-testid="stSelectbox"] [role="group"],
        [data-testid="stButton"] button,
        [data-testid="stButtonGroup"] button {
          border-radius: 0;
          min-height: 2.8rem;
          transition: transform 130ms ease, border-color 160ms ease, background-color 160ms ease;
        }
        [data-testid="stButton"] button:hover,
        [data-testid="stButtonGroup"] button:hover { border-color: var(--atlas-blue); }
        div[data-baseweb="select"] > div { border-color: var(--atlas-line) !important; }
        [data-testid="stSelectbox"] [role="group"] {
          border-color: var(--atlas-line) !important;
        }
        div[data-baseweb="select"]:focus-within > div {
          border-color: var(--atlas-blue) !important;
          box-shadow: inset 0 0 0 1px var(--atlas-blue) !important;
        }
        [data-testid="stSelectbox"] [role="group"][data-focus-within="true"] {
          border-color: var(--atlas-blue) !important;
          box-shadow: inset 0 0 0 1px var(--atlas-blue) !important;
        }
        [data-testid="stButtonGroup"] p { color: var(--atlas-ink) !important; }
        [data-testid="stButtonGroup"] button[aria-checked="true"],
        [data-testid="stButtonGroup"] button[aria-pressed="true"] {
          background: var(--atlas-blue) !important;
          border-color: var(--atlas-blue) !important;
          color: white !important;
        }
        [data-testid="stButtonGroup"] button[aria-checked="true"] p,
        [data-testid="stButtonGroup"] button[aria-pressed="true"] p { color: white !important; }
        [data-testid="stButton"] button:active,
        [data-testid="stButtonGroup"] button:active { transform: translateY(2px); }
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] p {
          color: var(--atlas-muted) !important;
          opacity: 1;
        }
        a { color: var(--atlas-blue); }
        a[href="#stMain"] { color: var(--atlas-ink) !important; background: white !important; }
        @keyframes atlas-data-arrival {
          0% { opacity: 0; transform: scaleX(0); }
          78% { opacity: 1; transform: scaleX(1); }
          100% { opacity: 0; transform: scaleX(1); }
        }
        @keyframes atlas-panel-in {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @keyframes atlas-track-in {
          from { transform: scaleX(0); }
          to { transform: scaleX(1); }
        }
        @keyframes atlas-endpoint-in {
          from { opacity: 0; transform: translate(-50%, -50%) scale(.55); }
          to { opacity: 1; transform: translate(-50%, -50%) scale(1); }
        }
        @keyframes atlas-plan-b-in {
          from { opacity: 0; transform: translateX(-8px); }
          to { opacity: 1; transform: translateX(0); }
        }
        @media (prefers-reduced-motion: reduce) {
          *, *::before, *::after { animation: none !important; transition: none !important; }
        }
        @media (max-width: 900px) {
          .block-container { padding: 1rem 1rem 3.5rem; }
          h1 { font-size: clamp(3.25rem, 15vw, 3.65rem) !important; }
          [data-testid="stVerticalBlock"]:has(
            > [data-testid="stElementContainer"] .atlas-header-anchor
          ) {
            margin-bottom: 1.4rem;
            padding: .7rem .75rem .85rem;
          }
          [data-testid="stHorizontalBlock"]:has(h1) {
            display: grid;
            gap: .55rem;
            grid-template-columns: 1fr;
          }
          [data-testid="stHorizontalBlock"]:has(h1) > [data-testid="stColumn"] {
            min-width: 0;
            width: auto !important;
          }
          .atlas-service-summary { gap: .55rem; grid-template-columns: .8fr 1.5fr; }
          .atlas-service-detail:last-child { display: none; }
          .atlas-service-status { font-size: .8rem; }
          .atlas-service-detail { font-size: .8rem; }
          .atlas-time-full { display: none !important; }
          .atlas-time-compact { display: block !important; }
          [data-testid="stHorizontalBlock"]:has([data-testid="stSelectbox"]) {
            display: grid;
            gap: .65rem;
            grid-template-columns: 1fr 1fr;
          }
          [data-testid="stHorizontalBlock"]:has([data-testid="stSelectbox"])
          > [data-testid="stColumn"] { min-width: 0; width: auto !important; }
          [data-testid="stHorizontalBlock"]:has([data-testid="stSelectbox"])
          > [data-testid="stColumn"]:nth-child(1) { grid-column: 1 / -1; }
          [data-testid="stButtonGroup"] > div,
          [data-testid="stButton"] button { width: 100%; }
          .atlas-task-line { margin: 0; }
          .atlas-task-line strong,
          .atlas-task-line span { display: inline; }
          .atlas-task-line span::before { content: ' '; }
          .atlas-map-heading { align-items: flex-start; flex-direction: column; }
          .map-legend {
            display: grid;
            grid-template-columns: repeat(2, max-content);
            justify-content: flex-start;
          }
          .map-legend span:last-child:nth-child(odd) { grid-column: 1 / -1; }
          .atlas-station-panel {
            margin-top: .15rem;
            padding: 1rem .95rem .85rem;
          }
          .atlas-station-head { align-items: start; padding-bottom: .85rem; }
          .atlas-secondary-stock { align-items: end; flex-direction: column; gap: 0; }
          .atlas-decision {
            gap: .75rem;
            grid-template-columns: minmax(5.4rem, .72fr) minmax(2.4rem, .32fr)
              minmax(8.5rem, 1.3fr);
            padding: .9rem 0;
          }
          .atlas-decision-track {
            height: 2px;
            justify-self: stretch;
            margin: 0;
            transform-origin: left center;
            width: auto;
          }
          .atlas-decision-track i { left: 0; top: 50%; }
          .atlas-decision-track b { left: 100%; top: 50%; }
          .atlas-decision-track span {
            display: none;
          }
          .atlas-decision-now strong { font-size: 3.1rem; }
          .atlas-decision-now em { font-size: .82rem; }
          .atlas-decision-now small,
          .atlas-decision-outlook small {
            font-size: .8rem;
            line-height: 1.45;
          }
          .atlas-decision-outlook > strong { font-size: 1.85rem; }
          .atlas-factors { padding-top: .8rem; }
          [class*="st-key-plan_b"] { padding: 1rem; }
          [class*="st-key-plan_b"] [data-testid="stHorizontalBlock"] {
            display: grid;
            grid-template-columns: 1fr;
          }
          [class*="st-key-plan_b"] [data-testid="stColumn"] {
            min-width: 0;
            width: auto !important;
          }
          .atlas-plan-b-copy {
            align-items: end;
            gap: .65rem 1rem;
            grid-template-columns: 1fr 1fr;
          }
          .atlas-plan-b-copy > span { grid-column: 1 / -1; }
          .atlas-plan-b-outlook {
            border-left: 1px solid rgba(49, 93, 255, .3);
            border-top: 0;
            margin: 0;
            padding: 0 0 0 1rem;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    config_path = os.getenv("BIKEPULSE_CONFIG", str(Path("config.example.toml")))
    _live_dashboard(config_path)


if __name__ == "__main__":
    main()
