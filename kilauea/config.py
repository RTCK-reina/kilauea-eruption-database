"""Project-wide configuration.

All paths are resolved relative to the package root so the CLI can be invoked
from anywhere.
"""
from __future__ import annotations

import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent
DATA_DIR = Path(os.environ.get("KILAUEA_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
LOG_DIR = Path(os.environ.get("KILAUEA_LOG_DIR", ROOT / "logs"))
DB_PATH = Path(os.environ.get("KILAUEA_DB", DATA_DIR / "kilauea.db"))

SCHEMA_SQL = PKG_DIR / "schema.sql"
VIEWS_SQL = PKG_DIR / "views.sql"

# --- Kīlauea reference values -------------------------------------------------
VNUM = "332010"              # Smithsonian GVP volcano number
HANS_VOLC_CD = "hi3"         # USGS HANS volcano code
SUMMIT_LAT = 19.421
SUMMIT_LON = -155.287
SUMMIT_ELEV_M = 1247

# Earthquake search cylinder around the summit. 30 km captures the summit
# reservoir, the upper/middle East Rift Zone and the Southwest Rift Zone.
QUAKE_RADIUS_KM = float(os.environ.get("KILAUEA_QUAKE_RADIUS_KM", 30.0))
QUAKE_START = os.environ.get("KILAUEA_QUAKE_START", "1959-01-01")

# --- Endpoints ----------------------------------------------------------------
GVP_WFS = "https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows"
USGS_EPISODES_URL = "https://www.usgs.gov/volcanoes/kilauea/science/eruption-information"
HANS_API = "https://volcanoes.usgs.gov/hans-public/api"
FDSN_EVENT = "https://earthquake.usgs.gov/fdsnws/event/1"
SCIENCEBASE_ITEM = "https://www.sciencebase.gov/catalog/item"
SCIENCEBASE_ITEMS = "https://www.sciencebase.gov/catalog/items"

USER_AGENT = (
    "kilauea-db/0.1 (research; volcano eruption database; "
    "contact: local user) python-requests"
)

# ScienceBase item IDs, resolved by search where possible but pinned here so a
# collection run is reproducible even if search ranking changes.
SB_TILT_ITEMS = [
    "5d8c0330e4b0c4f70d0c339a",  # 2018 eruption + earthquake sequence
    "6596f440d34e3265ab158d1a",  # 2020 (UWE, SDH)
    "66d76b3dd34eef5af66ca789",  # 2021
    "66d104dad34ebebd6af01eb4",  # 2022
    "67bfba13d34e8876fcbfca2c",  # 2023
    "67bfbc28d34e8876fcbfca43",  # 2024
    "67ead922d34ed02007f83585",  # 2025 Jan-Jun
    "654181f8d34ee4b6e05bcfcf",  # 2014 JKA (Aug 1 - Sep 15)
]

SB_SO2_ITEMS = [
    "5f1e975782cef313ed8e27dc",  # 2008-2013
    "5abb448be4b081f61abb68ae",  # 2014-2017
    "6529c152d34e44db0e2edbf1",  # 2018-2022
    "698fc90db66b01ea6aa36728",  # 2023-2025 traverse
]

SB_PLUME_ITEM = "6000a312d34e592d8671f57f"
SB_GRAVITY_ITEM = "5f73e77082cef8d1839962c7"
SB_THERMAL_ITEM = "61edcd20d34e8b818adb76c6"

# ScienceBase files above this size are raw spectra / imagery, not derived
# time series. Skipping them keeps the database analysis-ready.
SB_MAX_FILE_BYTES = int(os.environ.get("KILAUEA_SB_MAX_FILE_BYTES", 400 * 1024 * 1024))

# The continuous-gravity release is different: its per-year archives ARE the
# data (up to ~500 MB each), not raw spectra to skip. It gets its own ceiling so
# the general limit can stay tight enough to exclude the multi-gigabyte spectra
# and imagery bundles in the SO2 and plume releases.
SB_MAX_FILE_BYTES_GRAVITY = int(
    os.environ.get("KILAUEA_SB_MAX_GRAVITY_BYTES", 1024 * 1024 * 1024))

for _d in (DATA_DIR, RAW_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
