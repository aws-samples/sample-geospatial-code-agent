"""
Terminology:
 - cell: a cell of the source satellite grid (e.g. MGRS grid for Sentinel-2),
         identified by a grid code like "33UUP", covering ~100×100 km
 - scene: a single satellite acquisition/capture of a cell at a specific time,
          containing multiple spectral bands as separate assets
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
import os

import boto3
from shapely.geometry import shape, Polygon
import pystac_client
import numpy as np
import stackstac
import xarray as xr
import rioxarray
import rasterio


# For other STAC servers see: https://stacindex.org/catalogs?access=public&type=api
STAC_URL = "https://earth-search.aws.element84.com/v1"


SATELLITES = {
    "sentinel": {
        'collection': "sentinel-2-l2a",
        'start': date(2018, 12, 13)
    },
    "landsat": {
        'collection': "landsat-c2-l2",
        'start': date(1982, 8, 22),
        'rename_bands': {
            'nir08': 'nir'
        }
    },
    "naip": {
        'collection': "naip",
        'start': date(2010, 1, 1),
    },
}

GDAL_ENV = stackstac.rio_env.LayeredEnv({
    'AWS_REQUEST_PAYER': 'requester',
    'GDAL_HTTP_MULTIPLEX': 'YES',
    'GDAL_HTTP_VERSION': '2',
})


LOCATION_INDEX_NAME = os.environ.get('PLACE_INDEX_NAME', 'explore.place')


location_service_client = boto3.client('location')


def geocode(location_name: str) -> tuple[float, float] | None:
    """Convert location name to (lat, lon) using Amazon Location Service.
    Use this tool if the user named a place without providing its coordinates.
    
    Args:
        location_name: Name of the location to be geocoded.
    
    Returns:
        (lat, lon): Tuple with (Latitude in decimal degrees, Longitude in decimal degrees).
    """
    response = location_service_client.search_place_index_for_text(
        IndexName=LOCATION_INDEX_NAME,
        Text=location_name,
        MaxResults=1
    )
    if response['Results']:
        point = response['Results'][0]['Place']['Geometry']['Point']
        return (point[1], point[0])  # (lat, lon)
    
    return None


def search_satellite_scenes(
        satellite_name: str,
        polygon_coordinates: list[list[float]],
        start_date: date | None = None,
        end_date: date | None = None,
        days_delta: int = 30,
        max_cloud: float = 30) -> dict[str, list[dict[str, Any]]]:
    # Get the start and end dates
    if end_date is None:
        end_date = datetime.today().date()
    if start_date is None:
        start_date = end_date - timedelta(days=days_delta)
    
    satellite = SATELLITES[satellite_name]

    # Check start-date:
    if start_date < satellite['start']:
        raise ValueError(f"Warning: {satellite_name} data starts on {satellite['start']}.")

    # Search the scenes
    client = pystac_client.Client.open(STAC_URL)
    result = client.search(
        collections=[satellite['collection']],
        query={"eo:cloud_cover": {"lt": max_cloud}},
        intersects={
            "type": "Polygon",
            "coordinates": [polygon_coordinates]
          }, 
        datetime=f"{start_date.isoformat()}/{end_date.isoformat()}"
    )
    scenes = list(result.items_as_dicts())
    if not scenes:
        return {}

    # Divide the scenes by cell
    cell_scenes = defaultdict(list)
    for scene in scenes:
        cell_scenes[scene["properties"]["grid:code"]].append(scene)
    return cell_scenes


def check_bands(scene_dict: dict[str, Any], bands: list[str]) -> bool:
    available_bands = set(scene_dict['assets'].keys())
    return all(b in available_bands for b in bands)


def select_best_scene(
        coordinates: list[list[float]], 
        cell_scenes: dict[str, list[dict[str, Any]]],
        required_bands: list[str] | None = None) -> dict[str, Any]:
    
    # Filter out scenes missing required bands
    if required_bands:
        filtered_cell_scenes = {}
        for cell, scenes in cell_scenes.items():
            valid = [s for s in scenes if check_bands(s, required_bands)]
            if valid:
                filtered_cell_scenes[cell] = valid
        
        if not filtered_cell_scenes:
            raise ValueError(
                f"No scenes contain all requested bands: {required_bands}"
            )
        cell_scenes = filtered_cell_scenes

    # Find the tile with the biggest overlap with the area of interest
    aoi_shape = shape({
        "type": "Polygon",
        "coordinates": [coordinates]
    })
    aoi_area = aoi_shape.area
    coverage = []
    for cell, scenes in cell_scenes.items():
        cell_shape = shape(scenes[0]['geometry'])
        intersection = aoi_shape.intersection(cell_shape)
        coverage.append(((intersection.area / aoi_area), cell))
    max_coverage, max_coverage_cell = max(coverage)

    # Get the image with minimum cloud coverage
    # If the cloud coverage is equal, select the first one
    cloud_coverage = [(scene['properties']['eo:cloud_cover'], i, scene) for i, scene in enumerate(cell_scenes[max_coverage_cell])]
    min_cloud_coverage, _, scene = min(cloud_coverage)
    return {
        'cell': max_coverage_cell,
        'aoi_coverage': max_coverage,
        'cloud_coverage': min_cloud_coverage,
        'scene': scene,
    }


def fetch_scene_bands(
    satellite_name: str,
    scene_dict: dict[str, Any],
    polygon_coordinates: list[list[float]],
    bands: list[str],
    resolution: float | None = None,
    epsg: int | None = None
) -> xr.DataArray:
    polygon = Polygon(polygon_coordinates)

    data = stackstac.stack(
        items=[scene_dict],
        assets=bands,
        bounds_latlon=polygon.bounds,
        resolution=resolution,
        gdal_env=GDAL_ENV,
        epsg=epsg,
        dtype="float",
        fill_value=np.nan,
    )

    # Remove the time dimension, because it often confuses the Agent, and we have
    # the date already in the STAC item dictionary containing the scene metadata
    data = data.squeeze('time')

    # Apply band name mapping if provided, using the dictionary to look up new names
    rename_bands = SATELLITES[satellite_name].get('rename_bands')
    if rename_bands:
        new_band_names = [rename_bands.get(b, b) for b in data.coords['band'].values]
        data = data.assign_coords(band=new_band_names)

    data = data.compute()

    # Set CRS for rioxarray (stackstac stores CRS in attrs)
    data = data.rio.write_crs(data.attrs.get('crs'))

    # Clip to polygon geometry, setting outside values to NaN
    data = data.rio.clip(
        geometries=[polygon],
        crs="EPSG:4326",
        drop=False
    )
    return data


def get_satellite_data(
        satellite_name: str,
        bands: list[str], 
        polygon_coordinates: list[list[float]],
        start_date: date | None = None,
        end_date: date | None = None,
        days_delta: int = 30,
        max_cloud: float = 30) -> dict[str, Any]:
    """Retrieve satellite imagery for an area of interest.

    Orchestrates the complete workflow for acquiring satellite data:
    1. Searches for scenes intersecting the polygon within the date range
       and cloud cover threshold.
    2. Selects the best scene by maximizing AOI coverage and minimizing
       cloud cover.
    3. Fetches and loads the requested bands clipped to the polygon's
       bounding box.

    Args:
        satellite_name: The satellite platform to query. Must be a key in the
            SATELLITES dictionary {
                "sentinel": {
                    'collection': "sentinel-2-l2a",
                    'start': date(2018, 12, 13)
                },
                "landsat": {
                    'collection': "landsat-c2-l2",
                    'start': date(1982, 8, 22),
                    'rename_bands': {
                        'nir08': 'nir'
                    }
                },
            }
        bands: List of spectral band names to fetch. Band names must match
            the asset keys in the STAC item (e.g., ['red', 'green', 'blue',
            'nir'] for Sentinel-2, or ['red', 'green', 'blue', 'lwir11']
            for Landsat).
        polygon_coordinates: List of coordinate pairs defining the area of
            interest polygon vertices in [longitude, latitude] format. Must
            form a closed ring (first and last coordinates should match).
            Example: [[lon1, lat1], [lon2, lat2], [lon3, lat3], [lon1, lat1]]
        start_date: The start date for the search range. If None, defaults
            to `days_delta` days before `end_date`.
        end_date: The end date for the search range. If None, defaults to
            today's date.
        days_delta: Number of days before `end_date` to set as `start_date`
            when `start_date` is not provided. Defaults to 30.
        max_cloud: Maximum cloud cover percentage threshold (0-100). Only
            scenes with cloud cover below this value will be considered.
            Defaults to 30.

    Returns:
        A dictionary containing the selected scene metadata and imagery data
        with the following keys:
            - 'cell' (str): The grid cell code of the selected scene
              (e.g., "33UUP" for Sentinel-2 MGRS grid).
            - 'aoi_coverage' (float): Fraction of the AOI covered by the
              selected cell (0.0 to 1.0).
            - 'cloud_coverage' (float): Cloud cover percentage of the
              selected scene.
            - 'scene' (dict): The complete STAC item dictionary containing
              full metadata, geometry, and asset URLs.
            - 'data' (xr.DataArray): An xarray DataArray with dimensions
              (time, band, y, x) containing the requested bands, clipped
              to the AOI bounding box and loaded into memory.

    Example:
        >>> scene_data = get_satellite_data(
        ...     satellite="sentinel",
        ...     bands=["red", "green", "blue", "nir"],
        ...     polygon_coordinates=[
        ...         [11.0, 46.0], [11.5, 46.0],
        ...         [11.5, 46.5], [11.0, 46.5], [11.0, 46.0]
        ...     ],
        ...     max_cloud=20
        ... )
    """
    cell_scenes = search_satellite_scenes(satellite_name, polygon_coordinates, start_date, end_date, days_delta, max_cloud)
    total_scenes = sum(len(s) for s in cell_scenes.values())
    if total_scenes == 0:
        raise ValueError("No scenes found matching these criteria.")
    print(f"\nFound {total_scenes} scenes across {len(cell_scenes)} grid cells")
    for cell, scenes in cell_scenes.items():
        print(f"  Cell {cell}: {len(scenes)} scenes")
    
    # Use the specific band names for the given satellite
    rename_bands = SATELLITES[satellite_name].get('rename_bands')
    if rename_bands:
        generic_to_specific = {generic: specific for specific, generic in rename_bands.items()}
        bands = [generic_to_specific.get(b, b) for b in bands]

    best_scene = select_best_scene(polygon_coordinates, cell_scenes, bands)
    print("\nBest Scene Selected:")
    print(f"  Grid Cell: {best_scene['cell']}")
    print(f"  AOI Coverage: {best_scene['aoi_coverage']*100:.1f}%")
    print(f"  Cloud Coverage: {best_scene['cloud_coverage']:.1f}%")
    print(f"  Scene ID: {best_scene['scene']['id']}")
    print(f"  Date: {best_scene['scene']['properties']['datetime']}")

    scene_data = fetch_scene_bands(
        satellite_name,
        scene_dict=best_scene['scene'],
        polygon_coordinates=polygon_coordinates,
        bands=bands,
    )
    best_scene['data'] = scene_data
    print(f"\nData shape: {scene_data.shape}")
    print(f"Bands: {[str(b) for b in scene_data.band.values]}")
    print(f"CRS: {scene_data.attrs.get('crs', 'N/A')}")

    return best_scene


def get_high_resolution_image(
    polygon_coordinates: list[list[float]],
    start_date: date | None = None,
    end_date: date | None = None,
    resolution: float = 0.6,
) -> dict[str, Any]:
    """Retrieve a high-resolution aerial image suitable for visual inspection
    and object detection (e.g., cars, airplanes, ships, buildings).

    Uses NAIP (National Agriculture Imagery Program) imagery at 0.3-0.6m
    resolution. Coverage: contiguous United States only (2010-present).

    The returned RGB image has sufficient resolution to identify individual
    vehicles (~4m), aircraft (~30-70m), ships (~10-300m), and buildings.

    Args:
        polygon_coordinates: AOI polygon as [[lon, lat], ...] (closed ring).
            Keep the area small for object detection - ideally < 1 km².
            Large areas will produce very large images.
        start_date: Earliest acceptable image date. Defaults to 5 years ago.
        end_date: Latest acceptable image date. Defaults to today.
        resolution: Target resolution in meters. Default 0.6m. Use 0.3m for
            maximum detail (larger data). Must be >= 0.3.

    Returns:
        Dictionary with keys:
            - 'rgb' (np.ndarray): H x W x 3 uint8 RGB image array, ready for
              display or object detection model input.
            - 'nir' (np.ndarray): H x W float32 NIR band.
            - 'bounds' (tuple): (west, south, east, north) in EPSG:4326.
            - 'resolution' (float): Actual resolution in meters.
            - 'date' (str): Image capture date (ISO format).
            - 'scene_id' (str): NAIP scene identifier.
            - 'crs' (str): Coordinate reference system of the source data.

    Example:
        >>> result = get_high_resolution_image(
        ...     polygon_coordinates=[
        ...         [-77.04, 38.89], [-77.03, 38.89],
        ...         [-77.03, 38.88], [-77.04, 38.88], [-77.04, 38.89]
        ...     ],
        ...     resolution=0.6
        ... )
        >>> rgb = result['rgb']  # shape: (H, W, 3), dtype: uint8
        >>> print(f"Image size: {rgb.shape[1]}x{rgb.shape[0]} pixels")
    """
    if end_date is None:
        end_date = datetime.today().date()
    if start_date is None:
        start_date = end_date - timedelta(days=5 * 365)

    polygon = Polygon(polygon_coordinates)

    # Search NAIP scenes (no cloud cover filter — aerial imagery)
    client = pystac_client.Client.open(STAC_URL)
    result = client.search(
        collections=["naip"],
        intersects={
            "type": "Polygon",
            "coordinates": [polygon_coordinates]
        },
        datetime=f"{start_date.isoformat()}/{end_date.isoformat()}"
    )
    scenes = list(result.items_as_dicts())
    if not scenes:
        raise ValueError(
            "No NAIP imagery found for this area and date range. "
            "NAIP covers the contiguous United States only."
        )

    # Select the most recent scene with best AOI coverage
    aoi_shape = shape({"type": "Polygon", "coordinates": [polygon_coordinates]})
    scored = []
    for scene in scenes:
        scene_shape = shape(scene['geometry'])
        coverage = aoi_shape.intersection(scene_shape).area / aoi_shape.area
        scene_date = scene['properties']['datetime']
        scored.append((coverage, scene_date, scene))

    # Sort by coverage desc, then date desc (most recent)
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_scene = scored[0][2]

    print(f"Found {len(scenes)} NAIP scenes")
    print(f"Selected: {best_scene['id']}")
    print(f"  Date: {best_scene['properties']['datetime']}")
    print(f"  GSD: {best_scene['properties'].get('gsd', 'N/A')}m")

    # Load the image asset (single 4-band RGBNIR COG) using rioxarray
    # (stackstac doesn't support multi-band assets)
    image_href = best_scene['assets']['image']['href']
    epsg = best_scene['properties'].get('proj:epsg')

    env = rasterio.Env(AWS_REQUEST_PAYER='requester')
    with env:
        data = rioxarray.open_rasterio(image_href)

    # Clip to AOI bounding box in the raster's native CRS, then reproject
    data = data.rio.clip_box(*polygon.bounds, crs="EPSG:4326")
    if resolution != data.rio.resolution()[0]:
        data = data.rio.reproject(f"EPSG:{epsg}", resolution=resolution)
    data = data.rio.clip(geometries=[polygon], crs="EPSG:4326", drop=False)
    data = data.load()

    # Split into RGB and NIR, normalize with percentile stretch
    values = data.values  # shape: (4, H, W) — R, G, B, NIR
    nir = values[3]  # (H, W)

    # Valid pixel mask (not NaN / not outside polygon)
    mask_valid = np.isfinite(values[0])

    # Per-band percentile stretch for proper color rendering
    rgb_bands = []
    for i in range(3):
        band = values[i]
        valid_pixels = band[mask_valid]
        if valid_pixels.size == 0:
            rgb_bands.append(np.zeros_like(band, dtype=np.uint8))
            continue
        low = np.percentile(valid_pixels, 2)
        high = np.percentile(valid_pixels, 98)
        if high == low:
            rgb_bands.append(np.zeros_like(band, dtype=np.uint8))
            continue
        stretched = (band.astype(np.float32) - low) / (high - low)
        rgb_bands.append(np.clip(stretched * 255, 0, 255).astype(np.uint8))

    rgb_uint8 = np.stack(rgb_bands, axis=-1)  # (H, W, 3)
    # Set pixels outside polygon to white
    rgb_uint8[~mask_valid] = 255

    print(f"Image shape: {rgb_uint8.shape[1]}x{rgb_uint8.shape[0]} pixels at {resolution}m")

    return {
        'rgb': rgb_uint8,
        'nir': nir.astype(np.float32),
        'bounds': polygon.bounds,  # (west, south, east, north)
        'resolution': resolution,
        'date': best_scene['properties']['datetime'],
        'scene_id': best_scene['id'],
        'crs': f"EPSG:{epsg}",
    }
