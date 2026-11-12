"""Tests of the geospatial library used by the agent's Python REPL, run fully offline.

Scenes are served by the local STAC API of `offline_env` and backed by synthetic
GeoTIFFs, so the real numeric/geospatial stack touched by dependency updates runs
end to end: pystac / pystac-client, stackstac, rasterio (GDAL), rioxarray, xarray,
shapely, pyproj, numpy, matplotlib and Pillow, plus boto3 for Amazon Location.
"""
from datetime import date

import numpy as np
import pytest
import xarray as xr
from PIL import Image

from offline_env import (
    AOI_COORDINATES, AOI_FOLIUM_BOUNDS, BEST_SCENE_ID, EXPECTED_NDVI, EXPECTED_SURFACE_TEMPERATURE_C,
    GEOCODED_PLACE, PLACE_INDEX_NAME,
)
from geospatial import (
    ComputedIndex, compute_dNBR, compute_NBR, compute_NDVI, compute_NDWI, convert_lwir11_to_celsius,
    generate_overlay, get_satellite_data,
)
from geospatial import satellite_data
from geospatial.data_manipulation import classify_array, get_bands, safe_divide

JUNE_2024 = {"start_date": date(2024, 6, 1), "end_date": date(2024, 6, 30)}


def synthetic_scene(**bands):
    """A (band, y, x) DataArray like the one returned by `get_satellite_data`."""
    names = list(bands)
    values = np.stack([np.asarray(bands[n], dtype=float) for n in names])
    return xr.DataArray(values, dims=("band", "y", "x"), coords={"band": names})


# --- Satellite data retrieval (STAC search, scene selection, COG loading) -------

def test_get_satellite_data_selects_best_scene_and_loads_bands(stac_searches):
    result = get_satellite_data("sentinel", ["red", "nir"], AOI_COORDINATES, max_cloud=30, **JUNE_2024)

    # The STAC search request built by pystac-client.
    assert len(stac_searches) == 1
    search = stac_searches[0]
    assert search["collections"] == ["sentinel-2-l2a"]
    assert search["intersects"] == {"type": "Polygon", "coordinates": [AOI_COORDINATES]}
    assert search["query"] == {"eo:cloud_cover": {"lt": 30}}
    assert search["datetime"].startswith("2024-06-01") and "2024-06-30" in search["datetime"]

    # Best scene: biggest AOI coverage first, then lowest cloud cover.
    assert result["scene"]["id"] == BEST_SCENE_ID
    assert result["cell"] == "MGRS-30UXC"
    assert result["aoi_coverage"] == pytest.approx(1.0)
    assert result["cloud_coverage"] == 3.5

    # Bands loaded with stackstac/rasterio and clipped to the polygon with rioxarray.
    data = result["data"]
    assert isinstance(data, xr.DataArray)
    assert data.dims == ("band", "y", "x")
    assert [str(b) for b in data.band.values] == ["red", "nir"]
    assert data.rio.crs.to_epsg() == 32630
    red = data.sel(band="red").values
    assert np.isnan(red).any(), "pixels outside the polygon must be masked"
    assert np.nanmin(red) == np.nanmax(red) == 1000


def test_get_satellite_data_cloud_filter_and_errors():
    # Only the cloud-free scene of the neighbouring (partial coverage) cell is left.
    result = get_satellite_data("sentinel", ["red"], AOI_COORDINATES, max_cloud=1, **JUNE_2024)
    assert result["cell"] == "MGRS-31UCT"
    assert 0 < result["aoi_coverage"] < 1

    with pytest.raises(ValueError, match="No scenes contain all requested bands"):
        get_satellite_data("sentinel", ["red", "swir22"], AOI_COORDINATES, **JUNE_2024)
    with pytest.raises(ValueError, match="data starts on"):
        get_satellite_data("sentinel", ["red"], AOI_COORDINATES, start_date=date(2015, 1, 1), end_date=date(2015, 2, 1))


def test_landsat_band_renaming_and_thermal_conversion():
    result = get_satellite_data("landsat", ["red", "nir", "lwir11"], AOI_COORDINATES, **JUNE_2024)
    data = result["data"]
    # Landsat's `nir08` is exposed with the generic `nir` name.
    assert [str(b) for b in data.band.values] == ["red", "nir", "lwir11"]

    celsius = convert_lwir11_to_celsius(data)
    assert celsius.ndim == 2
    assert np.nanmean(celsius) == pytest.approx(EXPECTED_SURFACE_TEMPERATURE_C, abs=1e-3)
    assert compute_NDVI(data).get_statistics()["mean"] == pytest.approx(EXPECTED_NDVI, abs=1e-6)


def test_geocode_uses_amazon_location(aws_calls):
    lat, lon = satellite_data.geocode("Hyde Park, London")
    assert (lat, lon) == (GEOCODED_PLACE["lat"], GEOCODED_PLACE["lon"])

    [(name, request)] = aws_calls
    assert name == "location.SearchPlaceIndexForText"
    assert request["url_path"] == f"/places/v0/indexes/{PLACE_INDEX_NAME}/search/text"
    assert request["body"] == {"Text": "Hyde Park, London", "MaxResults": 1}


# --- Spectral indices -----------------------------------------------------------

def test_ndvi_statistics_classes_and_rendering():
    # Sentinel-2 L2A digital numbers (scaled by 1/10000 by `get_bands`), one NaN pixel.
    red = [[1000, 1000], [1000, np.nan]]
    nir = [[3000, 1000], [9000, np.nan]]
    ndvi = compute_NDVI(synthetic_scene(red=red, nir=nir))

    assert isinstance(ndvi, ComputedIndex)
    np.testing.assert_allclose(ndvi.values[0], [0.5, 0.0], atol=1e-6)
    assert ndvi.values[1, 0] == pytest.approx(0.8, abs=1e-6)
    assert np.isnan(ndvi.values[1, 1])

    stats = ndvi.get_statistics()
    assert stats["count"] == 3
    assert stats["mean"] == pytest.approx((0.5 + 0.0 + 0.8) / 3, abs=1e-6)
    assert stats["min"] == pytest.approx(0.0, abs=1e-6) and stats["max"] == pytest.approx(0.8, abs=1e-6)

    # Values on a class boundary fall in the upper class; NaN pixels are not classified.
    percentages = ndvi.get_class_percentages()
    assert percentages == pytest.approx({"Bare Soil/Rock": 100 / 3, "Dense Vegetation": 100 / 3, "Very Dense Vegetation": 100 / 3})

    rgba = ndvi.class_to_rgba()
    assert rgba.shape == (2, 2, 4)
    np.testing.assert_allclose(rgba[1, 1], [0, 0, 0, 0])  # nodata is transparent
    assert rgba[0, 0, 3] == 1.0


def test_other_indices():
    green, nir, swir22 = [[2000.0]], [[1000.0]], [[3000.0]]
    ndwi = compute_NDWI(synthetic_scene(green=green, nir=nir))
    assert ndwi.values[0, 0] == pytest.approx(1 / 3, abs=1e-6)

    nbr_before = compute_NBR(synthetic_scene(nir=[[5000.0]], swir22=[[1000.0]]))
    nbr_after = compute_NBR(synthetic_scene(nir=nir, swir22=swir22))
    assert nbr_before.values[0, 0] == pytest.approx(4 / 6, abs=1e-6)
    assert nbr_after.values[0, 0] == pytest.approx(-0.5, abs=1e-6)

    dnbr = compute_dNBR(nbr_before, nbr_after)
    assert isinstance(dnbr, ComputedIndex)
    assert dnbr.values[0, 0] == pytest.approx(4 / 6 + 0.5, abs=1e-6)


def test_data_manipulation_helpers():
    result = safe_divide(np.array([1.0, 1.0, 0.0]), np.array([2.0, 0.0, 0.0]))
    assert result[0] == 0.5 and np.isnan(result[1]) and np.isnan(result[2])

    bands = get_bands(synthetic_scene(red=[[5000.0]], nir=[[0.5]]), ["red", "nir"])
    assert bands["red"].dtype == np.float32 and bands["red"][0, 0] == pytest.approx(0.5)
    assert bands["nir"][0, 0] == pytest.approx(0.5)  # already reflectance: not rescaled
    with pytest.raises(ValueError, match="not found"):
        get_bands(synthetic_scene(red=[[1.0]]), ["nir"])

    np.testing.assert_array_equal(classify_array(np.array([0.1, 0.5, 0.9]), [0.0, 0.4, 1.0]), [0, 1, 1])
    with pytest.raises(ValueError):
        classify_array(np.array([-2.0]), [0.0, 1.0])


# --- Visualization ----------------------------------------------------------------

def test_generate_overlay_writes_transparent_png(tmp_path):
    data = np.linspace(-1, 1, 100).reshape(10, 10)
    data[0, 0] = np.nan
    path, bounds = generate_overlay(data, tmp_path / "overlay.png", AOI_COORDINATES, vmin=-1, vmax=1)

    assert bounds == AOI_FOLIUM_BOUNDS
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        width, height = image.size
        # The figure keeps the AOI bounding-box aspect ratio (wider than tall here).
        assert width > height > 0
