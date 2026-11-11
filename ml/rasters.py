"""Land cover next to each street from ESA WorldCover 10 m (2021, CC BY 4.0, open on AWS, no key).

Share of each land type within 50 m and 250 m of the middle of the street: built-up, trees, open fields (grass, crop,
shrub), bare ground and water. It says what lies beside the road (a wall of houses, a park, an empty plot) from a
satellite classification, not from what mappers happened to tag. A tile that can't be read leaves the columns empty
(unknown), never zero.
"""
import math

import numpy as np

URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{}_Map.tif"
CLASSES = {"built": (50,), "tree": (10,), "field": (20, 30, 40), "bare": (60,), "water": (80, 90, 95)}
RADII_M = (50, 250)
COLUMNS = [f"wc_{k}_{r}" for r in RADII_M for k in CLASSES]
NAN = float("nan")


def tile_name(lat, lon):
    """WorldCover tiles are 3 x 3 degrees, named by their lower-left corner, e.g. N27E075 covers Delhi."""
    la, lo = math.floor(lat / 3) * 3, math.floor(lon / 3) * 3
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


def shares(grid, rows, cols, radius_px):
    """Share of each class in a square of +-radius_px pixels around every (row, col); NaN where the square leaves the grid."""
    h, w = grid.shape
    r0, r1 = rows - radius_px, rows + radius_px + 1
    c0, c1 = cols - radius_px, cols + radius_px + 1
    ok = (r0 >= 0) & (c0 >= 0) & (r1 <= h) & (c1 <= w)
    r0, r1, c0, c1 = (np.clip(x, 0, n) for x, n in ((r0, h), (r1, h), (c0, w), (c1, w)))
    out = {}
    for name, codes in CLASSES.items():
        sat = np.pad(np.isin(grid, codes).astype(np.int64).cumsum(0).cumsum(1), ((1, 0), (1, 0)))  # summed-area table
        count = sat[r1, c1] - sat[r0, c1] - sat[r1, c0] + sat[r0, c0]
        out[name] = np.where(ok, count / ((2 * radius_px + 1) ** 2), np.nan)
    return out


def add_land_cover(rows):
    """Fills the wc_* columns of the street rows in place (each row has lat and lon). Needs rasterio and a network."""
    for r in rows:
        r.update({c: NAN for c in COLUMNS})
    try:
        import rasterio
        from rasterio.windows import from_bounds
    except ImportError:
        print("land cover: off (pip install rasterio)", flush=True)
        return 0
    by_tile = {}
    for r in rows:
        by_tile.setdefault(tile_name(r["lat"], r["lon"]), []).append(r)
    done = 0
    opts = dict(CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="4",
                GDAL_HTTP_RETRY_DELAY="2", GDAL_HTTP_TIMEOUT="40")
    for tile, group in by_tile.items():
        try:
            with rasterio.Env(**opts), rasterio.open("/vsicurl/" + URL.format(tile)) as ds:
                pad = max(RADII_M) / 111000 * 1.5
                lats, lons = [r["lat"] for r in group], [r["lon"] for r in group]
                win = from_bounds(min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad, ds.transform)
                win = win.round_offsets().round_lengths()
                grid = ds.read(1, window=win, boundless=True, fill_value=0)
                inv = ~ds.window_transform(win)
                res_m = abs(ds.res[1]) * 111000
        except Exception as err:  # rasterio raises many kinds of error for a missing or unreachable tile
            print(f"land cover {tile}: not read ({type(err).__name__}), left unknown", flush=True)
            continue
        cols, rws = zip(*(inv * (r["lon"], r["lat"]) for r in group))
        rws, cols = np.floor(rws).astype(int), np.floor(cols).astype(int)
        for radius in RADII_M:
            got = shares(grid, rws, cols, max(1, round(radius / res_m)))
            for k, name in enumerate(CLASSES):
                for r, v in zip(group, got[name]):
                    r[f"wc_{name}_{radius}"] = round(float(v), 3)
        done += len(group)
    return done
