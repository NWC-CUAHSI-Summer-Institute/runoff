"""
Aggregate NOAA StormEvents Flash Flood observations, match each merged event to a downstream USGS gage, sample MRMS radar coverage, and save one final CSV.

Main features:
- Processes one year or multiple years in one run.
- Pulls NOAA StormEvents details files directly from the NOAA website without saving input files locally.
- Prevents observations with different episode_id values from being merged.
- Does not cap merged event duration; long events are retained and flagged in the main CSV.
- Converts NOAA BEGIN_DATE_TIME and END_DATE_TIME to UTC
- Adds storm/gage hydrofabric IDs, selected downstream STAID, storm-gage distance, and radar coverage.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import rasterio
import requests
from tqdm.auto import tqdm


# ==========================================================
# USER SETTINGS
# ==========================================================

# Years to process, inclusive.
START_YEAR = 2021
END_YEAR = 2025

# Maximum time difference between report start times for possible merging.
TIME_WINDOW_HOURS = 24

# Duration threshold used only for QA/flagging long merged events.
# Events are NOT capped or filtered by this setting.
LONG_EVENT_THRESHOLD_HOURS = 48

# Extra distance buffer in km when checking whether report footprints overlap.
BUFFER_KM = 0.0

# Final output CSV. Use None to auto-name by year range.
# This is the only CSV written by the script.
OUTPUT_CSV = None

# Hydrofabric and gage inputs for the USGS matching step.
HYDROFABRIC_GPKG = "event_pipeline_inputs/conus_nextgen.gpkg"
GAGES_CSV = "event_pipeline_inputs/gages2_lt1000km2.csv"

# Radar coverage raster used to filter/select the downstream gage.
RADAR_RASTER = "event_pipeline_inputs/Radar_coverage_rfc_1km.tif"
RADAR_COVERAGE_THRESHOLD = 80.0

# If True, the final output keeps only events whose closest downstream gage
# has radar coverage >= RADAR_COVERAGE_THRESHOLD.
# If False, the final output keeps events with a closest downstream gage and
# includes radar_coverage as an added column without filtering by it.
FILTER_BY_RADAR_COVERAGE = True


# ==========================================================
# CONSTANTS
# ==========================================================

BASE_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
EVENT_TYPE_TO_KEEP = "Flash Flood"


# ==========================================================
# HELPERS
# ==========================================================

class UnionFind:
    """Small disjoint-set structure for joining observations into clusters."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a != root_b:
            self.parent[root_a] = root_b


def parse_damage(value) -> float:
    """Convert NOAA damage strings like 75.00K, 1.5M, 0, or blank to dollars."""
    if pd.isna(value):
        return np.nan

    text = str(value).strip().upper()
    if text == "":
        return np.nan

    multiplier = 1.0
    if text[-1] in "KMBT":
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[text[-1]]
        text = text[:-1]

    try:
        return float(text) * multiplier
    except ValueError:
        return np.nan


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in kilometers between two latitude/longitude points."""
    earth_radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * earth_radius_km * math.asin(math.sqrt(a))


def find_noaa_year_file(year: int) -> str:
    """Find the newest NOAA StormEvents details CSV.GZ filename for one year."""
    print(f"{year}: searching NOAA listing")
    response = requests.get(BASE_URL, timeout=60)
    response.raise_for_status()

    pattern = rf"StormEvents_details-ftp_v1\.0_d{year}_c[0-9]+\.csv\.gz"
    matches = sorted(set(re.findall(pattern, response.text)))

    if not matches:
        raise FileNotFoundError(f"No NOAA StormEvents details file found for {year}.")

    return matches[-1]


def load_stormevents(years: Iterable[int]) -> pd.DataFrame:
    """Load NOAA StormEvents details files directly from the NOAA website."""
    frames = []

    for year in years:
        filename = find_noaa_year_file(year)
        url = BASE_URL + filename
        print(f"{year}: reading {filename} from NOAA")

        df = pd.read_csv(url, compression="gzip", low_memory=False)
        df["source_file"] = filename
        frames.append(df)
        print(f"{year}: loaded {len(df):,} rows")

    if not frames:
        raise RuntimeError("No input files were loaded.")

    storm = pd.concat(frames, ignore_index=True)
    print(f"Total StormEvents rows loaded: {len(storm):,}")
    return storm

def require_columns(df: pd.DataFrame, required_columns: list[str]) -> None:
    """Stop early with a clear message if required NOAA columns are missing."""
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise KeyError("Missing required column(s): " + ", ".join(missing))



def parse_noaa_datetimes(series: pd.Series) -> pd.Series:
    """Parse NOAA datetime strings with a flexible fallback."""
    parsed = pd.to_datetime(series, format="%d-%b-%y %H:%M:%S", errors="coerce")

    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(series.loc[missing], errors="coerce")

    return parsed


def convert_noaa_time_columns_to_utc(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert NOAA local standard time columns to UTC in place.

    The column names are intentionally kept exactly as they are in the NOAA
    source file: BEGIN_DATE_TIME, END_DATE_TIME, and CZ_TIMEZONE. After this
    runs, BEGIN_DATE_TIME and END_DATE_TIME contain UTC timestamps and
    CZ_TIMEZONE is set to UTC.
    """
    for time_col in ["BEGIN_DATE_TIME", "END_DATE_TIME"]:
        local_time = parse_noaa_datetimes(df[time_col])

        # NOAA CZ_TIMEZONE values usually end with the UTC offset, e.g.
        # EST-5, CST-6, MST-7, PST-8, AKST-9, HST-10, SST-11, AST-4.
        offsets = (
            df["CZ_TIMEZONE"]
            .astype(str)
            .str.extract(r"([+-]?\d+(?:\.\d+)?)\s*$", expand=False)
            .astype(float)
        )

        # Local time = UTC + offset, so UTC = local time - offset.
        df[time_col] = local_time - pd.to_timedelta(offsets, unit="h")

    df["CZ_TIMEZONE"] = "UTC"
    return df


# ==========================================================
# PREPROCESSING
# ==========================================================

def preprocess_flash_floods(storm: pd.DataFrame) -> pd.DataFrame:
    """Filter to usable Flash Flood observations and create clustering fields."""
    required = [
        "EVENT_TYPE",
        "EPISODE_ID",
        "EVENT_ID",
        "BEGIN_DATE_TIME",
        "END_DATE_TIME",
        "CZ_TIMEZONE",
        "BEGIN_LAT",
        "BEGIN_LON",
        "END_LAT",
        "END_LON",
        "STATE",
        "WFO",
        "FLOOD_CAUSE",
        "DAMAGE_PROPERTY",
        "DAMAGE_CROPS",
        "DEATHS_DIRECT",
        "DEATHS_INDIRECT",
        "INJURIES_DIRECT",
        "INJURIES_INDIRECT",
    ]
    require_columns(storm, required)

    flash = storm[storm["EVENT_TYPE"] == EVENT_TYPE_TO_KEEP].copy()
    print(f"Flash Flood reports before cleanup: {len(flash):,}")

    numeric_columns = ["EPISODE_ID", "BEGIN_LAT", "BEGIN_LON", "END_LAT", "END_LON"]
    for col in numeric_columns:
        flash[col] = pd.to_numeric(flash[col], errors="coerce")

    # Convert NOAA local standard time values to UTC immediately while keeping
    # the original NOAA column names for direct comparison with the source data.
    flash = convert_noaa_time_columns_to_utc(flash)

    flash["damage_property_usd"] = flash["DAMAGE_PROPERTY"].apply(parse_damage)
    flash["damage_crops_usd"] = flash["DAMAGE_CROPS"].apply(parse_damage)

    flash["deaths_total"] = (
        flash["DEATHS_DIRECT"].fillna(0) + flash["DEATHS_INDIRECT"].fillna(0)
    )
    flash["injuries_total"] = (
        flash["INJURIES_DIRECT"].fillna(0) + flash["INJURIES_INDIRECT"].fillna(0)
    )

    needed_for_geometry = [
        "EPISODE_ID",
        "BEGIN_LAT",
        "BEGIN_LON",
        "END_LAT",
        "END_LON",
        "BEGIN_DATE_TIME",
    ]
    before = len(flash)
    flash = flash.dropna(subset=needed_for_geometry).reset_index(drop=True)
    print(f"Usable reports after cleanup: {len(flash):,}")
    print(f"Dropped unusable reports: {before - len(flash):,}")

    flash["episode_id"] = flash["EPISODE_ID"].astype("int64")
    flash["clat"] = (flash["BEGIN_LAT"] + flash["END_LAT"]) / 2.0
    flash["clon"] = (flash["BEGIN_LON"] + flash["END_LON"]) / 2.0

    flash["radius_km"] = flash.apply(
        lambda row: haversine_km(
            row["BEGIN_LAT"],
            row["BEGIN_LON"],
            row["END_LAT"],
            row["END_LON"],
        )
        / 2.0,
        axis=1,
    )

    return flash


# ==========================================================
# CLUSTERING
# ==========================================================

def reports_overlap(row_a, row_b, buffer_km: float) -> bool:
    """Return True when two report footprints overlap or touch with the buffer."""
    if abs(row_a.clat - row_b.clat) > 0.6:
        return False

    distance_km = haversine_km(row_a.clat, row_a.clon, row_b.clat, row_b.clon)
    overlap_limit_km = row_a.radius_km + row_b.radius_km + (2.0 * buffer_km)
    return distance_km <= overlap_limit_km


def cluster_one_episode(
    episode_reports: pd.DataFrame,
    hours: float,
    buffer_km: float,
) -> pd.Series:
    """
    Cluster observations inside one episode_id only, without a duration cap.

    Two reports can be joined when their BEGIN_DATE_TIME values are within
    the pairwise merge window and their spatial footprints overlap/touch.
    Union-find allows transitive chaining, so long events are retained rather
    than split or filtered. Long durations are flagged later for QA.
    """
    ordered = episode_reports.sort_values("BEGIN_DATE_TIME").reset_index()
    n = len(ordered)
    uf = UnionFind(n)

    merge_window = pd.Timedelta(hours=hours)
    start = 0

    for i in range(n):
        while ordered.loc[i, "BEGIN_DATE_TIME"] - ordered.loc[start, "BEGIN_DATE_TIME"] > merge_window:
            start += 1

        row_i = ordered.loc[i]
        for j in range(start, i):
            row_j = ordered.loc[j]
            if reports_overlap(row_i, row_j, buffer_km=buffer_km):
                uf.union(i, j)

    raw_roots = [uf.find(i) for i in range(n)]

    # Renumber roots sequentially within the episode for cleaner IDs.
    root_to_number = {}
    numbers = []
    for root in raw_roots:
        if root not in root_to_number:
            root_to_number[root] = len(root_to_number) + 1
        numbers.append(root_to_number[root])

    labels = pd.Series(index=episode_reports.index, dtype="object")
    labels.loc[ordered["index"].values] = numbers
    return labels


def cluster_by_episode(
    flash: pd.DataFrame,
    hours: float,
    buffer_km: float,
) -> pd.Series:
    """Assign cluster IDs without ever merging across episode_id values."""
    labels = pd.Series(index=flash.index, dtype="object")

    for episode_id, group in flash.groupby("episode_id", sort=False):
        local_labels = cluster_one_episode(
            group,
            hours=hours,
            buffer_km=buffer_km,
        )
        labels.loc[group.index] = [
            f"{int(episode_id)}_{int(label)}" for label in local_labels.loc[group.index]
        ]

    return labels


# ==========================================================
# AGGREGATION
# ==========================================================

def aggregate_events(flash: pd.DataFrame) -> pd.DataFrame:
    """Collapse clustered observations into one output row per event."""
    records = []

    for cluster_id, group in flash.groupby("cluster"):
        episode_ids = group["episode_id"].dropna().unique()
        if len(episode_ids) != 1:
            raise ValueError(
                f"Cluster {cluster_id} contains multiple episode_id values: {episode_ids}"
            )

        begin = group["BEGIN_DATE_TIME"].min()

        # Use NOAA END_DATE_TIME for the reported event end/duration.
        # This value has already been converted to UTC while keeping the
        # original NOAA column name. No duration cap is applied here.
        end = group["END_DATE_TIME"].dropna().max()
        if pd.isna(end):
            end = group["BEGIN_DATE_TIME"].max()

        # Guard against malformed records where END_DATE_TIME is earlier than
        # BEGIN_DATE_TIME. In that case, fall back to the latest report begin time.
        if pd.notna(end) and pd.notna(begin) and end < begin:
            end = group["BEGIN_DATE_TIME"].max()

        duration_hours = (
            (end - begin).total_seconds() / 3600.0
            if pd.notna(begin) and pd.notna(end)
            else np.nan
        )

        centroid_lat = group["clat"].mean()
        centroid_lon = group["clon"].mean()

        footprint_radius_km = max(
            haversine_km(centroid_lat, centroid_lon, row.clat, row.clon)
            + row.radius_km
            for row in group.itertuples()
        )

        property_damage = group["damage_property_usd"].sum(min_count=1)
        crop_damage = group["damage_crops_usd"].sum(min_count=1)

        records.append(
            {
                "cluster_id": cluster_id,
                "episode_id": int(episode_ids[0]),
                "YEAR": int(begin.year) if pd.notna(begin) else np.nan,
                "n_reports": len(group),
                "BEGIN_DATE_TIME": begin,
                "END_DATE_TIME": end,
                "CZ_TIMEZONE": "UTC",
                "duration_hours": round(duration_hours, 2)
                if pd.notna(duration_hours)
                else np.nan,
                "duration_gt_48h": bool(duration_hours > LONG_EVENT_THRESHOLD_HOURS)
                if pd.notna(duration_hours)
                else False,
                "centroid_lat": round(centroid_lat, 4),
                "centroid_lon": round(centroid_lon, 4),
                "footprint_radius_km": round(footprint_radius_km, 2),
                "n_states": group["STATE"].nunique(),
                "states": ", ".join(sorted(group["STATE"].dropna().astype(str).unique())),
                "primary_state": group["STATE"].mode().iat[0]
                if not group["STATE"].mode().empty
                else None,
                "n_wfos": group["WFO"].nunique(),
                "wfos": ", ".join(sorted(group["WFO"].dropna().astype(str).unique())),
                "deaths_total": int(group["deaths_total"].sum()),
                "injuries_total": int(group["injuries_total"].sum()),
                "damage_property_usd": property_damage,
                "damage_crops_usd": crop_damage,
                "damage_total_usd": np.nansum([property_damage, crop_damage]),
                "flood_causes": ", ".join(
                    sorted(group["FLOOD_CAUSE"].dropna().astype(str).unique())
                ),
                "event_ids": ", ".join(group["EVENT_ID"].astype(str)),
                "source_files": ", ".join(
                    sorted(group["source_file"].dropna().astype(str).unique())
                ),
            }
        )

    events = pd.DataFrame(records)
    if events.empty:
        return events

    return events.sort_values(
        ["BEGIN_DATE_TIME", "episode_id", "n_reports"],
        ascending=[True, True, False],
    ).reset_index(drop=True)




# ==========================================================
# HYDROFABRIC, USGS GAGES, AND RADAR COVERAGE
# ==========================================================

def read_hydrofabric(hydrofabric_gpkg: str):
    """Read only the hydrofabric layers/columns needed for storm-gage matching."""
    gpkg = Path(hydrofabric_gpkg)
    if not gpkg.exists():
        raise FileNotFoundError(f"Hydrofabric GeoPackage not found: {gpkg}")

    print(f"Reading hydrofabric: {gpkg}")

    network = gpd.read_file(
        gpkg,
        layer="network",
        columns=["id", "toid", "divide_id", "vpuid"],
        read_geometry=False,
    )

    divides = gpd.read_file(
        gpkg,
        layer="divides",
        columns=["divide_id", "geometry"],
    )

    flowpaths = gpd.read_file(
        gpkg,
        layer="flowpaths",
        columns=["id", "geometry"],
    )

    network["id"] = network["id"].astype(str)
    network["toid"] = network["toid"].astype(str)
    network["divide_id"] = network["divide_id"].astype(str)
    flowpaths["id"] = flowpaths["id"].astype(str)
    divides["divide_id"] = divides["divide_id"].astype(str)

    return network, divides, flowpaths


def build_flowpath_graph(network: pd.DataFrame) -> nx.DiGraph:
    """Build directed hydrofabric graph where id -> toid follows downstream flow."""
    network_edges = network.dropna(subset=["id", "toid"]).copy()
    network_edges = network_edges[
        (network_edges["id"].astype(str).str.strip() != "")
        & (network_edges["toid"].astype(str).str.strip() != "")
        & (network_edges["toid"].astype(str).str.lower() != "nan")
    ]

    graph = nx.from_pandas_edgelist(
        network_edges,
        source="id",
        target="toid",
        create_using=nx.DiGraph(),
    )

    print(f"Hydrofabric graph: {graph.number_of_nodes():,} nodes, {graph.number_of_edges():,} edges")
    return graph


def attach_storm_hydrofabric_ids(
    events: pd.DataFrame,
    network: pd.DataFrame,
    divides: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Add storm_cat-id and storm_flowpath-id to the merged event table."""
    required = ["centroid_lon", "centroid_lat"]
    require_columns(events, required)

    storm_points = gpd.GeoDataFrame(
        events.copy(),
        geometry=gpd.points_from_xy(events["centroid_lon"], events["centroid_lat"]),
        crs="EPSG:4326",
    ).to_crs(divides.crs)

    joined = gpd.sjoin(
        storm_points,
        divides[["divide_id", "geometry"]],
        how="left",
        predicate="within",
    ).drop(columns=["index_right"], errors="ignore")

    divide_to_flowpath = (
        network[["divide_id", "id"]]
        .dropna()
        .drop_duplicates(subset="divide_id")
        .set_index("divide_id")["id"]
    )

    joined["storm_cat-id"] = joined["divide_id"]
    joined["storm_flowpath-id"] = joined["storm_cat-id"].map(divide_to_flowpath)

    out = pd.DataFrame(joined.drop(columns=["geometry", "divide_id"], errors="ignore"))
    matched = out["storm_flowpath-id"].notna().sum()
    print(f"Storms matched to hydrofabric flowpaths: {matched:,} / {len(out):,}")
    return out


def read_and_snap_gages(
    gages_csv: str,
    network: pd.DataFrame,
    flowpaths: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Read USGS gages, snap them to nearest flowpath, and add gage hydrologic IDs."""
    path = Path(gages_csv)
    if not path.exists():
        raise FileNotFoundError(f"USGS gage CSV not found: {path}")

    print(f"Reading USGS gages: {path}")
    gages = pd.read_csv(path, dtype={"STAID": str}, low_memory=False)
    require_columns(gages, ["STAID", "LAT_GAGE", "LNG_GAGE"])

    # Use STAID as the only USGS station identifier. Preserve leading zeros
    # for standard 8-character USGS gage IDs without truncating longer IDs.
    gages["STAID"] = gages["STAID"].astype(str).str.strip().str.zfill(8)

    gages_gdf = gpd.GeoDataFrame(
        gages,
        geometry=gpd.points_from_xy(gages["LNG_GAGE"], gages["LAT_GAGE"]),
        crs="EPSG:4326",
    ).to_crs(flowpaths.crs)

    snapped = gpd.sjoin_nearest(
        gages_gdf,
        flowpaths[["id", "geometry"]],
        how="left",
        distance_col="gage_snap_dist_m",
    ).rename(columns={"id": "gage_flowpath-id"})

    snapped["gage_flowpath-id"] = snapped["gage_flowpath-id"].astype(str)

    flowpath_to_divide = (
        network[["id", "divide_id"]]
        .dropna()
        .drop_duplicates(subset="id")
        .set_index("id")["divide_id"]
    )
    snapped["gage_cat-id"] = snapped["gage_flowpath-id"].map(flowpath_to_divide)

    matched = snapped["gage_flowpath-id"].notna().sum()
    print(f"Gages snapped to flowpaths: {matched:,} / {len(snapped):,}")
    return snapped


def select_closest_downstream_gage(
    events_with_storm_ids: pd.DataFrame,
    gages_indexed: gpd.GeoDataFrame,
    graph: nx.DiGraph,
) -> pd.DataFrame:
    """For each storm event, select the closest downstream gage, including the same flowpath."""
    valid_events = events_with_storm_ids[
        events_with_storm_ids["storm_flowpath-id"].notna()
    ].copy()

    gage_lookup = gages_indexed[
        gages_indexed["gage_flowpath-id"].notna()
    ].copy()

    records = []
    print("Selecting closest downstream gage for each storm event")

    for _, event in tqdm(valid_events.iterrows(), total=len(valid_events)):
        event_dict = event.to_dict()
        storm_fp = str(event_dict["storm_flowpath-id"])

        if storm_fp not in graph:
            continue

        downstream_flowpaths = nx.descendants(graph, storm_fp)
        downstream_flowpaths.add(storm_fp)

        downstream_gages = gage_lookup[
            gage_lookup["gage_flowpath-id"].isin(downstream_flowpaths)
        ].copy()

        if downstream_gages.empty:
            continue

        # Compute straight-line distance from the storm centroid to each
        # candidate downstream gage in meters. This is a Euclidean distance
        # in EPSG:5070 and is separate from the network downstream relation.
        storm_point_5070 = gpd.GeoSeries(
            gpd.points_from_xy([event_dict["centroid_lon"]], [event_dict["centroid_lat"]]),
            crs="EPSG:4326",
        ).to_crs("EPSG:5070").iloc[0]

        downstream_gages_5070 = downstream_gages.to_crs("EPSG:5070")
        downstream_gages = downstream_gages.copy()
        downstream_gages["storm_gage_dist_m"] = (
            downstream_gages_5070.geometry.distance(storm_point_5070).astype(float)
        )

        closest = downstream_gages.sort_values("storm_gage_dist_m").iloc[0]

        record = dict(event_dict)
        record.update(
            {
                "STAID": closest.get("STAID"),
                "DRAIN_SQKM": closest.get("DRAIN_SQKM", np.nan),
                "gage_lat": closest.get("LAT_GAGE"),
                "gage_lon": closest.get("LNG_GAGE"),
                "gage_cat-id": closest.get("gage_cat-id"),
                "gage_flowpath-id": closest.get("gage_flowpath-id"),
                "gage_snap_dist_m": closest.get("gage_snap_dist_m"),
                "storm_gage_dist_m": closest.get("storm_gage_dist_m"),
            }
        )
        records.append(record)

    out = pd.DataFrame(records)
    print(f"Events with a downstream gage: {len(out):,} / {len(events_with_storm_ids):,}")
    return out


def add_radar_coverage(event_gage_rows: pd.DataFrame, radar_raster: str) -> pd.DataFrame:
    """Sample radar coverage at the selected gage location."""
    path = Path(radar_raster)
    if not path.exists():
        raise FileNotFoundError(f"Radar raster not found: {path}")

    if event_gage_rows.empty:
        event_gage_rows["radar_coverage"] = np.nan
        return event_gage_rows

    print(f"Sampling radar coverage at gage locations: {path}")
    gage_points = gpd.GeoDataFrame(
        event_gage_rows.copy(),
        geometry=gpd.points_from_xy(event_gage_rows["gage_lon"], event_gage_rows["gage_lat"]),
        crs="EPSG:4326",
    )

    with rasterio.open(path) as src:
        nodata = src.nodata
        gage_points_raster = gage_points.to_crs(src.crs)
        coords = [(geom.x, geom.y) for geom in gage_points_raster.geometry]
        coverage = [value[0] for value in src.sample(coords)]

    out = pd.DataFrame(gage_points.drop(columns=["geometry"], errors="ignore"))
    out["radar_coverage"] = coverage
    if nodata is not None:
        out.loc[out["radar_coverage"] == nodata, "radar_coverage"] = np.nan

    return out


def attach_usgs_and_radar(events: pd.DataFrame) -> pd.DataFrame:
    """
    Add storm/gage hydrofabric IDs, selected downstream STAID, storm-gage distance, and radar coverage.

    The original columns from the NOAA merge output are left unchanged. New columns
    are appended to the final table.
    """
    network, divides, flowpaths = read_hydrofabric(HYDROFABRIC_GPKG)
    graph = build_flowpath_graph(network)

    events_with_storm_ids = attach_storm_hydrofabric_ids(events, network, divides)
    gages_indexed = read_and_snap_gages(GAGES_CSV, network, flowpaths)

    event_gage_rows = select_closest_downstream_gage(
        events_with_storm_ids,
        gages_indexed,
        graph,
    )

    event_gage_rows = add_radar_coverage(event_gage_rows, RADAR_RASTER)

    if FILTER_BY_RADAR_COVERAGE:
        before = len(event_gage_rows)
        event_gage_rows = event_gage_rows[
            event_gage_rows["radar_coverage"] >= RADAR_COVERAGE_THRESHOLD
        ].copy()
        print(
            f"Events retained after radar coverage >= {RADAR_COVERAGE_THRESHOLD:g}%: "
            f"{len(event_gage_rows):,} / {before:,}"
        )

    if "STATID" in event_gage_rows.columns:
        event_gage_rows = event_gage_rows.drop(columns=["STATID"])

    if "storm_gage_dist_m" in event_gage_rows.columns:
        missing_distance = event_gage_rows["storm_gage_dist_m"].isna().sum()
        print(f"Rows with missing storm_gage_dist_m: {missing_distance:,}")

    return event_gage_rows.reset_index(drop=True)

def choose_output_path(years: list[int]) -> Path:
    """Use the configured output path or generate one from the year range."""
    if OUTPUT_CSV:
        return Path(OUTPUT_CSV)

    if len(years) == 1:
        return Path(f"flashflood_final_events_{years[0]}.csv")

    return Path(f"flashflood_final_events_{min(years)}_{max(years)}.csv")


# ==========================================================
# MAIN RUN
# ==========================================================

def main() -> None:
    years = list(range(START_YEAR, END_YEAR + 1))

    print("Starting Flash Flood event aggregation")
    print(f"Years: {min(years)}-{max(years)}")
    print(f"Pairwise merge window: {TIME_WINDOW_HOURS} hours")
    print(f"Long-event QA threshold: {LONG_EVENT_THRESHOLD_HOURS} hours")
    print("Maximum event duration: not capped")
    print("NOAA input files are read directly from the website and not saved locally")
    print(f"Distance buffer: {BUFFER_KM} km")

    storm = load_stormevents(years)
    flash = preprocess_flash_floods(storm)

    flash["cluster"] = cluster_by_episode(
        flash,
        hours=TIME_WINDOW_HOURS,
        buffer_km=BUFFER_KM,
    )

    print(
        f"{len(flash):,} usable reports merged into "
        f"{flash['cluster'].nunique():,} episode-safe events"
    )

    events = aggregate_events(flash)
    final_rows = attach_usgs_and_radar(events)

    output_path = choose_output_path(years)
    final_rows.to_csv(output_path, index=False)

    multi_report_events = int((events["n_reports"] > 1).sum()) if not events.empty else 0
    largest_event = int(events["n_reports"].max()) if not events.empty else 0
    long_events = int(events["duration_gt_48h"].sum()) if not events.empty else 0

    print("Done")
    print(f"Saved final CSV: {output_path.resolve()}")
    print(f"Merged NOAA events before gage/radar filtering: {len(events):,}")
    print(f"Final event-gage rows: {len(final_rows):,}")
    print(f"Multi-report events: {multi_report_events:,}")
    print(f"Largest event: {largest_event:,} reports")
    print(f"Events longer than {LONG_EVENT_THRESHOLD_HOURS} hours: {long_events:,}")


if __name__ == "__main__":
    main()
