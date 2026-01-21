# app.py
import io
import os
import re
import zipfile
import tempfile

import pandas as pd
import streamlit as st

import geopandas as gpd
from shapely.geometry import Point

import folium
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

st.set_page_config(page_title="CSV → Shapefile (Points)", layout="centered")

# --- Persist preview state across reruns ---
if "show_preview" not in st.session_state:
    st.session_state.show_preview = False

st.title("CSV → Shapefile Converter (Latitude/Longitude)")
st.write(
    "Upload a CSV containing latitude & longitude columns (plus any other attributes). "
    "Preview points on a basemap, then download as a **point shapefile**."
)

# ---------- Helpers ----------
def normalize_col(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).strip().lower())

LAT_ALIASES = {"lat", "latitude", "y", "ycoord", "ycoordinate"}
LON_ALIASES = {"lon", "long", "longitude", "x", "xcoord", "xcoordinate"}

def guess_lat_lon_columns(df: pd.DataFrame):
    norm_map = {c: normalize_col(c) for c in df.columns}
    lat_candidates = [c for c, n in norm_map.items() if n in LAT_ALIASES]
    lon_candidates = [c for c, n in norm_map.items() if n in LON_ALIASES]
    lat_col = lat_candidates[0] if lat_candidates else None
    lon_col = lon_candidates[0] if lon_candidates else None
    return lat_col, lon_col

def safe_shapefile_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Shapefile constraints:
      - max 10 characters for column names
      - avoid duplicate names after truncation
    """
    new_cols = []
    used = set()
    for c in df.columns:
        base = re.sub(r"[^A-Za-z0-9_]", "_", str(c))[:10]
        if not base:
            base = "field"
        candidate = base
        i = 1
        while candidate.lower() in used:
            suffix = str(i)
            candidate = (base[: max(0, 10 - len(suffix))] + suffix)[:10]
            i += 1
        used.add(candidate.lower())
        new_cols.append(candidate)
    out = df.copy()
    out.columns = new_cols
    return out

def to_download_zip(folder_path: str) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(folder_path):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, folder_path)
                zf.write(full, arcname=rel)
    mem.seek(0)
    return mem.read()

def build_gdf_from_csv(df: pd.DataFrame, lat_col: str, lon_col: str) -> gpd.GeoDataFrame:
    work = df.copy()
    work[lat_col] = pd.to_numeric(work[lat_col], errors="coerce")
    work[lon_col] = pd.to_numeric(work[lon_col], errors="coerce")
    work = work.dropna(subset=[lat_col, lon_col])

    gdf = gpd.GeoDataFrame(
        work,
        geometry=[Point(xy) for xy in zip(work[lon_col], work[lat_col])],
        crs="EPSG:4326"  # assumes input lat/lon degrees
    )
    return gdf

def preview_map(gdf_wgs84: gpd.GeoDataFrame, popup_cols=None):
    # Center
    center_lat = float(gdf_wgs84.geometry.y.mean())
    center_lon = float(gdf_wgs84.geometry.x.mean())

    m = folium.Map(location=[center_lat, center_lon], zoom_start=8, control_scale=True)

    # Safe tiles (avoid attribution errors)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap", show=True).add_to(m)
    folium.TileLayer("CartoDB positron", name="CartoDB Positron", show=False).add_to(m)
    folium.TileLayer("CartoDB dark_matter", name="CartoDB Dark Matter", show=False).add_to(m)

    cluster = MarkerCluster(name="Points").add_to(m)

    # Popup fields
    if popup_cols:
        popup_cols = [c for c in popup_cols if c in gdf_wgs84.columns and c != "geometry"]
        popup_cols = popup_cols[:6]
    else:
        popup_cols = []

    for _, row in gdf_wgs84.iterrows():
        lat = float(row.geometry.y)
        lon = float(row.geometry.x)

        popup_html = ""
        if popup_cols:
            lines = [f"<b>{c}</b>: {row.get(c, '')}" for c in popup_cols]
            popup_html = "<br>".join(lines)

        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(popup_html, max_width=320) if popup_html else None,
            tooltip="Point"
        ).add_to(cluster)

    # Zoom to bounds
    try:
        bounds = gdf_wgs84.total_bounds  # [minx, miny, maxx, maxy]
        m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
    except Exception:
        pass

    folium.LayerControl(collapsed=True).add_to(m)
    return m

# ---------- UI ----------
uploaded = st.file_uploader("Upload CSV", type=["csv"])
sep = st.selectbox("CSV separator", options=[",", ";", "\t", "|"], index=0)
encoding = st.selectbox("Encoding", options=["utf-8", "utf-8-sig", "latin1", "cp1252"], index=0)

if uploaded is not None:
    try:
        df = pd.read_csv(uploaded, sep=sep, encoding=encoding)
    except Exception as e:
        st.error(f"Could not read the CSV. Error: {e}")
        st.stop()

    st.subheader("Preview (Table)")
    st.dataframe(df.head(25), use_container_width=True)

    if df.empty:
        st.warning("CSV is empty.")
        st.stop()

    guessed_lat, guessed_lon = guess_lat_lon_columns(df)

    st.subheader("Select Latitude/Longitude columns")
    c1, c2 = st.columns(2)
    with c1:
        lat_col = st.selectbox(
            "Latitude column",
            options=df.columns.tolist(),
            index=df.columns.get_loc(guessed_lat) if guessed_lat in df.columns else 0,
            key="lat_col",
        )
    with c2:
        lon_col = st.selectbox(
            "Longitude column",
            options=df.columns.tolist(),
            index=df.columns.get_loc(guessed_lon) if guessed_lon in df.columns else 0,
            key="lon_col",
        )

    # If user changes key columns, keep preview visible but it will refresh map
    other_cols = [c for c in df.columns if c not in [lat_col, lon_col]]

    st.subheader("Map Preview Options")
    popup_cols = st.multiselect(
        "Columns to show in point popup (optional)",
        options=other_cols,
        default=[],
        key="popup_cols",
    )

    crs_opt = st.selectbox("Output CRS", ["EPSG:4326 (WGS84)", "Custom EPSG"], index=0)
    if crs_opt.startswith("EPSG:4326"):
        out_crs = "EPSG:4326"
    else:
        epsg = st.text_input("Enter EPSG code (e.g., 4326, 32643, 3857)", value="4326")
        out_crs = f"EPSG:{epsg.strip()}"

    # Buttons
    b1, b2, b3 = st.columns([1, 1, 1])
    with b1:
        if st.button("Preview on Map"):
            st.session_state.show_preview = True
    with b2:
        if st.button("Hide Preview"):
            st.session_state.show_preview = False
    with b3:
        convert_btn = st.button("Convert to Shapefile")

    # Build WGS84 GDF for preview + conversion
    try:
        gdf_wgs84 = build_gdf_from_csv(df, lat_col, lon_col)
    except Exception as e:
        st.error(f"Failed to create points from lat/lon: {e}")
        st.stop()

    if len(gdf_wgs84) == 0:
        st.error("No valid rows after cleaning lat/lon (missing or non-numeric).")
        st.stop()

    # Persisted preview
    if st.session_state.show_preview:
        st.subheader("Map Preview")

        # Range warning for WGS84 degrees
        bad = gdf_wgs84[
            (gdf_wgs84.geometry.y.abs() > 90) |
            (gdf_wgs84.geometry.x.abs() > 180)
        ]
        if len(bad) > 0:
            st.warning(
                f"{len(bad)} points look out-of-range for WGS84 degrees (lat ±90, lon ±180). "
                "If your coordinates are projected (meters), the preview will look wrong."
            )

        m = preview_map(gdf_wgs84, popup_cols=popup_cols)
        st_folium(m, width=900, height=520)

    # Conversion
    if convert_btn:
        st.subheader("Shapefile Output")

        gdf_out = gdf_wgs84.copy()

        # Reproject output if requested
        try:
            if out_crs != "EPSG:4326":
                gdf_out = gdf_out.to_crs(out_crs)
        except Exception as e:
            st.error(f"CRS reprojection failed: {e}")
            st.stop()

        # Ensure shapefile-friendly columns
        attrs = gdf_out.drop(columns="geometry")
        attrs_safe = safe_shapefile_columns(attrs)
        gdf_safe = gpd.GeoDataFrame(attrs_safe, geometry=gdf_out.geometry, crs=gdf_out.crs)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_name = "points_from_csv"
            shp_path = os.path.join(tmpdir, f"{out_name}.shp")

            try:
                gdf_safe.to_file(shp_path, driver="ESRI Shapefile")
            except Exception as e:
                st.error(
                    "Failed to write shapefile. Install a working GeoPandas I/O backend "
                    "(`pyogrio` recommended; or `fiona`).\n\n"
                    f"Error: {e}"
                )
                st.stop()

            zip_bytes = to_download_zip(tmpdir)

        st.success(f"Created shapefile with {len(gdf_safe)} points.")
        st.download_button(
            "Download Shapefile (ZIP)",
            data=zip_bytes,
            file_name="shapefile_from_csv.zip",
            mime="application/zip",
        )

else:
    st.markdown("### CSV format example")
    st.code(
        "Station,Latitude,Longitude,Type,Value\n"
        "A,30.284,77.985,RainGauge,12.5\n"
        "B,30.290,77.990,River,NA\n"
    )
