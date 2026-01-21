# app.py
import io
import os
import re
import zipfile
import tempfile

import pandas as pd
import streamlit as st

# Geospatial
import geopandas as gpd
from shapely.geometry import Point

st.set_page_config(page_title="CSV → Shapefile (Points)", layout="centered")

st.title("CSV → Shapefile Converter (Latitude/Longitude)")
st.write(
    "Upload a CSV containing latitude & longitude columns (plus any other attributes). "
    "This app will generate a **point shapefile** and give you a ZIP to download."
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
    Shapefile field constraints:
      - max 10 characters for column names
      - limited types (strings truncated, etc.)
    We'll rename columns safely and avoid duplicates.
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
    df2 = df.copy()
    df2.columns = new_cols
    return df2

def to_download_zip(folder_path: str) -> bytes:
    """
    Zip all files in folder_path.
    """
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(folder_path):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, folder_path)
                zf.write(full, arcname=rel)
    mem.seek(0)
    return mem.read()

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

    st.subheader("Preview")
    st.dataframe(df.head(20), use_container_width=True)

    if df.empty:
        st.warning("CSV is empty.")
        st.stop()

    guessed_lat, guessed_lon = guess_lat_lon_columns(df)

    st.subheader("Select Latitude/Longitude columns")
    col1, col2 = st.columns(2)
    with col1:
        lat_col = st.selectbox("Latitude column", options=df.columns.tolist(), index=df.columns.get_loc(guessed_lat) if guessed_lat in df.columns else 0)
    with col2:
        lon_col = st.selectbox("Longitude column", options=df.columns.tolist(), index=df.columns.get_loc(guessed_lon) if guessed_lon in df.columns else 0)

    crs_opt = st.selectbox("Output CRS", ["EPSG:4326 (WGS84)", "Custom EPSG"], index=0)
    if crs_opt.startswith("EPSG:4326"):
        out_crs = "EPSG:4326"
    else:
        epsg = st.text_input("Enter EPSG code (e.g., 4326, 32643, 3857)", value="4326")
        out_crs = f"EPSG:{epsg.strip()}"

    st.info(
        "Tip: If your coordinates are in degrees (lat/lon), keep EPSG:4326. "
        "If they're projected (meters), pick the correct EPSG."
    )

    make_btn = st.button("Convert to Shapefile")

    if make_btn:
        work = df.copy()

        # Coerce to numeric
        work[lat_col] = pd.to_numeric(work[lat_col], errors="coerce")
        work[lon_col] = pd.to_numeric(work[lon_col], errors="coerce")

        # Drop invalid
        before = len(work)
        work = work.dropna(subset=[lat_col, lon_col])
        after = len(work)

        if after == 0:
            st.error("No valid rows found after removing missing/non-numeric lat/lon.")
            st.stop()

        st.write(f"Valid points: **{after}** (dropped {before-after} invalid rows)")

        # Basic sanity check for EPSG:4326 ranges
        if out_crs == "EPSG:4326":
            bad = work[(work[lat_col].abs() > 90) | (work[lon_col].abs() > 180)]
            if len(bad) > 0:
                st.warning(
                    f"{len(bad)} rows look out-of-range for WGS84 degrees (lat ±90, lon ±180). "
                    "If your coords are projected, choose the correct EPSG."
                )

        # Create GeoDataFrame
        gdf = gpd.GeoDataFrame(
            work,
            geometry=[Point(xy) for xy in zip(work[lon_col], work[lat_col])],
            crs="EPSG:4326"  # assumes input is lat/lon degrees; see note below
        )

        # Reproject if needed
        try:
            if out_crs != "EPSG:4326":
                gdf = gdf.to_crs(out_crs)
        except Exception as e:
            st.error(f"CRS reprojection failed: {e}")
            st.stop()

        # Shapefile constraints: rename long columns etc.
        # IMPORTANT: Geometry must stay 'geometry'
        attrs = gdf.drop(columns="geometry")
        attrs_safe = safe_shapefile_columns(attrs)
        gdf_safe = gpd.GeoDataFrame(attrs_safe, geometry=gdf.geometry, crs=gdf.crs)

        # Write shapefile to temp folder
        with tempfile.TemporaryDirectory() as tmpdir:
            out_name = "points_from_csv"
            shp_path = os.path.join(tmpdir, f"{out_name}.shp")

            try:
                # geopandas will use pyogrio/fiona depending on what's installed
                gdf_safe.to_file(shp_path, driver="ESRI Shapefile")
            except Exception as e:
                st.error(
                    "Failed to write shapefile. Make sure geopandas has a working I/O backend "
                    "(install `pyogrio` or `fiona`).\n\n"
                    f"Error: {e}"
                )
                st.stop()

            zip_bytes = to_download_zip(tmpdir)

        st.success("Shapefile created!")
        st.download_button(
            "Download Shapefile (ZIP)",
            data=zip_bytes,
            file_name="shapefile_from_csv.zip",
            mime="application/zip",
        )

        st.caption(
            "Note: This app assumes the input lat/lon are WGS84 degrees (EPSG:4326). "
            "If your input is already projected, tell me and I’ll modify the app to treat input CRS correctly."
        )
else:
    st.markdown("### CSV format example")
    st.code(
        "Station,Latitude,Longitude,Type,Value\n"
        "A,30.284,77.985,RainGauge,12.5\n"
        "B,30.290,77.990,River,NA\n"
    )

st.markdown("---")
st.write("If you want, I can add a small map preview and a coordinate validation report.")
