"""Offline harness for running the Geospatial agent service without AWS or the internet.

Used by the pytest suite (via conftest.py) and by `e2e_agent_server.py`, which
serves the agent to the web UI end-to-end tests.

The agent normally talks to Bedrock, S3, Amazon Location Service and the public
Element84 Earth Search STAC API (whose items point to Cloud-Optimized GeoTIFFs on
S3). In CI none of those are reachable, so importing this module:

* points every AWS SDK at fake credentials (no request can ever reach real AWS),
* starts `moto` and creates the file-sharing bucket used by `share_file_with_client`,
* answers Amazon Location `SearchPlaceIndexForText` through a botocore `before-call`
  hook. The hook runs *after* botocore has validated and serialized the request
  against the installed service model, so a boto3/botocore bump that breaks the
  call shape still fails,
* serves a minimal STAC API on 127.0.0.1 whose items reference small synthetic
  GeoTIFFs written to a temporary directory, and points the geospatial library at
  it. The real `pystac-client`, `stackstac`, `rasterio`/GDAL, `rioxarray`, `shapely`
  and `xarray` code paths therefore run unmodified.

It also provides `ScriptedModel`, a deterministic stand-in for the Bedrock LLM
that drives the real Strands / strands-code-agent loop with pre-defined tool calls.

This module must be imported before `geospatial` / `geospatial_agent`, because
several of their modules create AWS clients at import time.
"""
import atexit
import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- 1. Isolate from any real AWS configuration (must happen before boto3 import)
for _var in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
    os.environ.pop(_var, None)

FILE_SHARING_BUCKET = "client-file-sharing-test"
PLACE_INDEX_NAME = "geospatial-test-place-index"

os.environ.update({
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
    # Keep OpenTelemetry quiet/offline if the distro is auto-loaded.
    "OTEL_SDK_DISABLED": "true",
    # Configuration injected by the AgentCore stack (infrastructure/stacks/agentcore_stack.py).
    "CLIENT_FILE_SHARING_BUCKET_NAME": FILE_SHARING_BUCKET,
    "PLACE_INDEX_NAME": PLACE_INDEX_NAME,
    # GDAL must never try to reach the network for the local test rasters.
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "MPLBACKEND": "Agg",
})

import boto3  # noqa: E402
import botocore.handlers  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from botocore.awsrequest import AWSResponse  # noqa: E402
from moto import mock_aws  # noqa: E402
from pyproj import Transformer  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402
from shapely.geometry import Polygon, mapping  # noqa: E402

from strands.models import Model  # noqa: E402


REGION = "us-east-1"

# Area of interest (Hyde Park, London) in [lon, lat], closed ring: the same polygon
# is pasted into the UI by the end-to-end tests (user-interface/e2e/support/scenario.ts).
AOI_COORDINATES = [
    [-0.1750, 51.5030],
    [-0.1550, 51.5030],
    [-0.1550, 51.5110],
    [-0.1750, 51.5110],
    [-0.1750, 51.5030],
]
AOI_WEST, AOI_SOUTH, AOI_EAST, AOI_NORTH = Polygon(AOI_COORDINATES).bounds
# Folium bounds returned by `generate_overlay` for the AOI: [[south, west], [north, east]].
AOI_FOLIUM_BOUNDS = [[AOI_SOUTH, AOI_WEST], [AOI_NORTH, AOI_EAST]]

GEOCODED_PLACE = {"label": "Hyde Park, London, GBR", "lon": -0.1657, "lat": 51.5073}


# --- 2. Hooks for the AWS calls moto cannot emulate ---------------------------
# Every request reaching these hooks has been validated and serialized by
# botocore; it is recorded so tests can assert on what the library sent.
AWS_CALLS = []


def _ok():
    # Same (http_response, parsed) contract used by botocore.stub.Stubber.
    return AWSResponse(None, 200, {}, None)


def _fake_search_place_index_for_text(params, **_):
    # `params` is the serialized HTTP request (rest-json): the body is the JSON payload.
    AWS_CALLS.append(("location.SearchPlaceIndexForText", {"url_path": params["url_path"], "body": json.loads(params["body"])}))
    response = {
        "Summary": {"Text": json.loads(params["body"])["Text"], "DataSource": "Esri"},
        "Results": [{
            "Place": {"Label": GEOCODED_PLACE["label"], "Geometry": {"Point": [GEOCODED_PLACE["lon"], GEOCODED_PLACE["lat"]]}},
            "Relevance": 1.0,
        }],
    }
    return _ok(), response


# BUILTIN_HANDLERS are copied into every botocore session created afterwards,
# including sessions created internally by strands / bedrock-agentcore.
botocore.handlers.BUILTIN_HANDLERS.extend([
    ("before-call.location.SearchPlaceIndexForText", _fake_search_place_index_for_text),
])


# --- 3. Start moto and create the resources used by the agent -----------------
MOCK = mock_aws()
MOCK.start()
boto3.client("s3", region_name=REGION).create_bucket(Bucket=FILE_SHARING_BUCKET)


# --- 4. Synthetic satellite scenes (Cloud-Optimized GeoTIFFs on local disk) ---
DATA_DIR = tempfile.mkdtemp(prefix="geospatial-test-data-")
atexit.register(shutil.rmtree, DATA_DIR, ignore_errors=True)

UTM_EPSG = 32630  # WGS 84 / UTM zone 30N, as Sentinel-2 / Landsat tiles over London
PIXEL_SIZE = 10.0
SCENE_DATE = "2024-06-15T11:06:21.024000Z"

# Surface reflectance values (Sentinel-2 L2A digital numbers, scale 1/10000).
# NDVI = (0.30 - 0.10) / (0.30 + 0.10) = 0.5 for the best (least cloudy) scene.
BEST_SCENE_ID = "S2B_30UXC_20240615_0_L2A"
EXPECTED_NDVI = 0.5
EXPECTED_SURFACE_TEMPERATURE_C = 27.0


def _write_cog(path, value, transform, shape, dtype):
    data = np.full(shape, value, dtype=dtype)
    profile = {
        "driver": "GTiff", "height": shape[0], "width": shape[1], "count": 1, "dtype": dtype,
        "crs": f"EPSG:{UTM_EPSG}", "transform": transform,
        "tiled": True, "blockxsize": 256, "blockysize": 256, "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def _scene_grid():
    """A UTM raster grid covering the AOI with a margin, and its footprint in lon/lat."""
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{UTM_EPSG}", always_xy=True)
    to_lonlat = Transformer.from_crs(f"EPSG:{UTM_EPSG}", "EPSG:4326", always_xy=True)
    xs, ys = to_utm.transform([c[0] for c in AOI_COORDINATES], [c[1] for c in AOI_COORDINATES])
    margin = 500.0
    west = np.floor((min(xs) - margin) / PIXEL_SIZE) * PIXEL_SIZE
    north = np.ceil((max(ys) + margin) / PIXEL_SIZE) * PIXEL_SIZE
    east = np.ceil((max(xs) + margin) / PIXEL_SIZE) * PIXEL_SIZE
    south = np.floor((min(ys) - margin) / PIXEL_SIZE) * PIXEL_SIZE
    shape = (int((north - south) / PIXEL_SIZE), int((east - west) / PIXEL_SIZE))
    transform = from_origin(west, north, PIXEL_SIZE, PIXEL_SIZE)
    corners = [(west, south), (east, south), (east, north), (west, north), (west, south)]
    footprint = Polygon([to_lonlat.transform(x, y) for x, y in corners])
    return transform, shape, (west, south, east, north), footprint


def _stac_item(collection, item_id, cell, cloud_cover, bands, footprint=None):
    transform, shape, utm_bbox, grid_footprint = _scene_grid()
    footprint = footprint or grid_footprint
    assets = {}
    for band, (value, dtype) in bands.items():
        path = os.path.join(DATA_DIR, f"{item_id}_{band}.tif")
        _write_cog(path, value, transform, shape, dtype)
        assets[band] = {"href": path, "type": "image/tiff; application=geotiff; profile=cloud-optimized", "roles": ["data"]}
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "collection": collection,
        "bbox": list(footprint.bounds),
        "geometry": mapping(footprint),
        "properties": {
            "datetime": SCENE_DATE,
            "eo:cloud_cover": cloud_cover,
            "grid:code": cell,
            "proj:epsg": UTM_EPSG,
            "proj:shape": list(shape),
            "proj:transform": list(transform)[:6],
            "proj:bbox": list(utm_bbox),
        },
        "assets": assets,
        "links": [],
    }


def _build_catalog():
    _, _, _, footprint = _scene_grid()
    # A neighbouring cell that only clips the eastern edge of the AOI.
    minx, miny, maxx, maxy = footprint.bounds
    sliver = Polygon([(AOI_EAST - 0.002, miny), (maxx, miny), (maxx, maxy), (AOI_EAST - 0.002, maxy)])
    sentinel = lambda nir: {"red": (1000, "uint16"), "green": (800, "uint16"), "blue": (600, "uint16"), "nir": (nir, "uint16")}  # noqa: E731
    return {
        "sentinel-2-l2a": [
            # Same cell, cloudier acquisition (NDVI 0): must NOT be selected.
            _stac_item("sentinel-2-l2a", "S2A_30UXC_20240612_0_L2A", "MGRS-30UXC", 25.0, sentinel(1000)),
            # Best scene: full AOI coverage, lowest cloud cover (NDVI 0.5).
            _stac_item("sentinel-2-l2a", BEST_SCENE_ID, "MGRS-30UXC", 3.5, sentinel(3000)),
            # Neighbouring cell with partial coverage and no clouds: must NOT be selected.
            _stac_item("sentinel-2-l2a", "S2B_31UCT_20240615_0_L2A", "MGRS-31UCT", 0.0, sentinel(100), footprint=sliver),
        ],
        "landsat-c2-l2": [
            # Landsat names the NIR band `nir08` (renamed to `nir` by the library); lwir11 in Kelvin.
            _stac_item("landsat-c2-l2", "LC09_L2SP_201024_20240615_02_T1", "WRS-201024", 10.0, {
                "red": (1000, "uint16"), "nir08": (3000, "uint16"),
                "lwir11": (273.15 + EXPECTED_SURFACE_TEMPERATURE_C, "float32"),
            }),
        ],
    }


CATALOG = _build_catalog()


# --- 5. Minimal STAC API (landing page + POST /search) ------------------------
STAC_SEARCHES = []


class _StacHandler(BaseHTTPRequestHandler):
    def _send_json(self, body, status=200, content_type="application/json"):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "":
            base = f"http://{self.headers['Host']}"
            return self._send_json({
                "type": "Catalog", "stac_version": "1.0.0", "id": "geospatial-test", "description": "Offline STAC API",
                "conformsTo": [
                    "https://api.stacspec.org/v1.0.0/core",
                    "https://api.stacspec.org/v1.0.0/item-search",
                    "https://api.stacspec.org/v1.0.0/item-search#query",
                ],
                "links": [
                    {"rel": "self", "href": base, "type": "application/json"},
                    {"rel": "root", "href": base, "type": "application/json"},
                    {"rel": "search", "href": f"{base}/search", "type": "application/geo+json", "method": "POST"},
                ],
            })
        self._send_json({"code": "NotFound", "description": self.path}, status=404)

    def do_POST(self):  # noqa: N802
        if self.path != "/search":
            return self._send_json({"code": "NotFound", "description": self.path}, status=404)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        STAC_SEARCHES.append(body)
        features = [item for collection in body.get("collections", []) for item in CATALOG.get(collection, [])]
        max_cloud = body.get("query", {}).get("eo:cloud_cover", {}).get("lt")
        if max_cloud is not None:
            features = [f for f in features if f["properties"]["eo:cloud_cover"] < max_cloud]
        self._send_json({"type": "FeatureCollection", "features": features, "links": []}, content_type="application/geo+json")

    def log_message(self, *args):
        pass


STAC_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _StacHandler)
STAC_URL = f"http://127.0.0.1:{STAC_SERVER.server_address[1]}"
threading.Thread(target=STAC_SERVER.serve_forever, daemon=True, name="offline-stac").start()


def _point_library_to_offline_stac():
    from geospatial import satellite_data
    satellite_data.STAC_URL = STAC_URL


_point_library_to_offline_stac()


def stop():
    STAC_SERVER.shutdown()
    MOCK.stop()


# --- 6. Deterministic LLM ------------------------------------------------------
def tool_use(name, tool_input, tool_use_id=None):
    return {"toolUse": {"name": name, "input": tool_input, "toolUseId": tool_use_id or f"tooluse_{name}"}}


def text(content):
    return {"text": content}


class ScriptedModel(Model):
    """A Strands `Model` that replays one scripted assistant turn per `stream()` call.

    Each turn is a list of content blocks built with `tool_use(...)` / `text(...)`.
    The real Strands event loop executes the requested tools and calls `stream()`
    again with the tool results, which are recorded in `self.requests`.
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.config = {"model_id": "scripted"}

    def update_config(self, **model_config):
        self.config.update(model_config)

    def get_config(self):
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.requests.append({"messages": messages, "tool_specs": tool_specs, "system_prompt": system_prompt})
        if not self.turns:
            raise AssertionError("ScriptedModel ran out of scripted turns")
        blocks = self.turns.pop(0)

        yield {"messageStart": {"role": "assistant"}}
        stop_reason = "end_turn"
        for block in blocks:
            if "toolUse" in block:
                stop_reason = "tool_use"
                tu = block["toolUse"]
                yield {"contentBlockStart": {"start": {"toolUse": {"name": tu["name"], "toolUseId": tu["toolUseId"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tu["input"])}}}}
            else:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": stop_reason}}
        yield {"metadata": {"usage": {"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200}, "metrics": {"latencyMs": 1}}}


# --- 7. Shared end-to-end analysis scenario -----------------------------------
SCENARIO_QUESTION = "What is the NDVI in this area?"
SCENARIO_INTERMEDIATE_TEXT = "I'll fetch the least cloudy Sentinel-2 scene and compute the NDVI."
SCENARIO_ANSWER = "The mean NDVI in the area is 0.50, indicating moderate to dense vegetation."
OVERLAY_NAME = "ndvi_overlay"
IMAGE_NAME = "ndvi_classes"
CSV_NAME = "ndvi_statistics.csv"


def analysis_scenario(work_dir):
    """Return (turns, analysis_code) for a realistic NDVI analysis touching every agent capability:
    STAC search + COG loading, spectral index computation, a map raster overlay, a matplotlib
    image, a Plotly chart, a file shared through S3, and a final answer.
    """
    work_dir = str(work_dir)
    overlay_path = os.path.join(work_dir, f"{OVERLAY_NAME}.png")
    image_path = os.path.join(work_dir, f"{IMAGE_NAME}.png")
    chart_path = os.path.join(work_dir, "ndvi_chart.json")
    csv_path = os.path.join(work_dir, CSV_NAME)
    analysis_code = f"""
result = get_satellite_data('sentinel', ['red', 'nir'], AOI_COORDINATES, start_date=date(2024, 6, 1), end_date=date(2024, 6, 30))
ndvi = compute_NDVI(result['data'])
stats = ndvi.get_statistics()
print(f"SCENE={{result['scene']['id']}} NDVI_MEAN={{stats['mean']:.3f}}")
overlay_path, bounds = generate_overlay(ndvi.values, '{overlay_path}', AOI_COORDINATES, cmap=ndvi.index.index_cmap, vmin=-1, vmax=1)
print(f"BOUNDS={{bounds}}")
classes = ndvi.get_class_percentages()
fig = px.bar(x=list(classes.keys()), y=list(classes.values()), title='NDVI classes (%)')
fig.write_json('{chart_path}')
plt.figure(figsize=(4, 4))
plt.imshow(ndvi.class_to_rgba())
plt.axis('off')
plt.savefig('{image_path}')
pd.DataFrame([stats]).to_csv('{csv_path}', index=False)
"""
    turns = [
        [text(SCENARIO_INTERMEDIATE_TEXT), tool_use("python_repl", {"code": analysis_code})],
        [
            tool_use("visualize_map_raster_layer", {"image_path": overlay_path, "folium_bounds": AOI_FOLIUM_BOUNDS}, "tooluse_overlay"),
            tool_use("visualize_image", {"image_path": image_path}, "tooluse_image"),
            tool_use("visualize_interactive_chart", {"chart_path": chart_path, "chart_type": "plotly"}, "tooluse_chart"),
            tool_use("share_file_with_client", {"file_path": csv_path}, "tooluse_share"),
        ],
        [text(SCENARIO_ANSWER)],
    ]
    return turns, analysis_code
