"""Smoke tests for the agent service, run fully offline (see conftest.py).

They exercise the real dependency stack that dependency/security updates touch:
bedrock-agentcore (HTTP app + SSE streaming), strands-agents (event loop, tool
execution), strands-code-agent + smolagents (sandboxed Python REPL), the geospatial
library (pystac-client, stackstac, rasterio, rioxarray, xarray, shapely), numpy,
pandas, matplotlib, plotly, Pillow and boto3/botocore (S3 file sharing).

The SSE events asserted here are the contract consumed by the web UI parser in
`user-interface/src/services/api.ts`. The same scenario is served to the UI
end-to-end tests by `e2e_agent_server.py` (see `user-interface/e2e/full-stack.spec.ts`).
"""
import base64
import io
import json
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from PIL import Image
from starlette.testclient import TestClient

from offline_env import (
    AOI_COORDINATES, AOI_FOLIUM_BOUNDS, BEST_SCENE_ID, CSV_NAME, FILE_SHARING_BUCKET, IMAGE_NAME, OVERLAY_NAME,
    SCENARIO_ANSWER, SCENARIO_INTERMEDIATE_TEXT, SCENARIO_QUESTION, ScriptedModel, analysis_scenario,
)

SESSION_ID = "e2e-session-0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def service():
    from geospatial_agent import agent_service
    return agent_service


@pytest.fixture
def client(service):
    with TestClient(service.app) as test_client:
        yield test_client


def read_sse_events(response):
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def decode_png(b64):
    with Image.open(io.BytesIO(base64.b64decode(b64))) as image:
        assert image.format == "PNG"
        return image.size


def test_ping(client):
    response = client.get("/ping")
    assert response.status_code == 200


def test_solution_user_agent_is_registered(service):
    from geospatial_agent.solution_user_agent import SOLUTION_UA

    session = boto3.session.Session()
    assert SOLUTION_UA in (session._session.user_agent_extra or "")


def test_invocation_streams_full_analysis(client, monkeypatch, tmp_path, stac_searches):
    from geospatial_agent import agent
    from geospatial_agent.bedrock_models import DEFAULT_MODEL_ID

    turns, analysis_code = analysis_scenario(tmp_path)
    model = ScriptedModel(turns)
    model_kwargs = {}

    def fake_bedrock_model(**kwargs):
        model_kwargs.update(kwargs)
        return model

    monkeypatch.setattr(agent, "BedrockModel", fake_bedrock_model)

    with client.stream(
        "POST", "/invocations",
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": SESSION_ID},
        json={
            "message": SCENARIO_QUESTION,
            "coordinates": AOI_COORDINATES,
            "history": [["user", "Hello"], ["assistant", "Hi, draw an area and ask me about it."]],
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = read_sse_events(response)

    errors = [e for e in events if "error" in e]
    assert not errors, f"Agent stream reported errors: {errors}"

    by_type = {}
    for event in events:
        by_type.setdefault((event.get("msg_type"), event.get("name")), []).append(event)

    # 1. Intermediate text.
    assert any(e.get("msg_type") == "text" and e["text"] == SCENARIO_INTERMEDIATE_TEXT for e in events)

    # 2. Sandboxed Python REPL: STAC search, COG loading, NDVI, overlay, chart, image, CSV.
    assert by_type[("toolUse", "python_repl")][0]["input"]["code"] == analysis_code
    repl_output = by_type[("toolResult", "python_repl")][0]["content"][0]["text"]
    assert f"SCENE={BEST_SCENE_ID} NDVI_MEAN=0.500" in repl_output, repl_output
    assert f"BOUNDS={AOI_FOLIUM_BOUNDS}" in repl_output, repl_output
    assert "STDERR" not in repl_output, repl_output
    assert stac_searches and stac_searches[0]["intersects"]["coordinates"] == [AOI_COORDINATES]

    # 3. UI tools: images/charts are inlined in the toolUse event, their results are not streamed.
    overlay = by_type[("toolUse", "visualize_map_raster_layer")][0]
    assert overlay["input"]["folium_bounds"] == AOI_FOLIUM_BOUNDS
    assert overlay["input"]["image_path"].endswith(f"{OVERLAY_NAME}.png")
    width, height = decode_png(overlay["image"])
    assert width > height > 0

    image = by_type[("toolUse", "visualize_image")][0]
    assert image["input"]["image_path"].endswith(f"{IMAGE_NAME}.png")
    decode_png(image["image"])

    chart = by_type[("toolUse", "visualize_interactive_chart")][0]
    chart_spec = json.loads(chart["chart_spec"])
    assert chart_spec["data"][0]["type"] == "bar"
    assert chart_spec["data"][0]["x"] == ["Dense Vegetation"]

    for ui_tool in ("visualize_map_raster_layer", "visualize_image", "visualize_interactive_chart"):
        assert ("toolResult", ui_tool) not in by_type

    # 4. File shared through S3 with a pre-signed URL scoped to the session.
    shared = by_type[("toolUse", "share_file_with_client")][0]
    url = urlparse(shared["pre_signed_s3_url"])
    assert FILE_SHARING_BUCKET in url.netloc + url.path
    assert url.path.endswith(f"{SESSION_ID}/{CSV_NAME}")
    query = parse_qs(url.query)
    assert "X-Amz-Signature" in query or "Signature" in query, url.query  # SigV4 or (us-east-1) SigV2 query auth
    body = boto3.client("s3").get_object(Bucket=FILE_SHARING_BUCKET, Key=f"{SESSION_ID}/{CSV_NAME}")["Body"].read().decode()
    assert body.splitlines()[0].startswith("min,max,mean,median")

    # 5. Final result with metrics.
    assert any(e.get("msg_type") == "text" and e["text"] == SCENARIO_ANSWER for e in events)
    result = events[-1]
    assert result["msg_type"] == "result"
    assert SCENARIO_ANSWER in result["result"]["response"]
    agent_metrics = result["result"]["metrics"]["agent"]
    assert agent_metrics["total_cycles"] == 3
    assert agent_metrics["on_demand_cost"] > 0

    # The model configuration, conversation history and tools are passed to the LLM.
    # The default model (Claude 5.5) rejects `temperature`, so it must not be sent.
    assert model_kwargs == {"model_id": DEFAULT_MODEL_ID}
    first_messages = model.requests[0]["messages"]
    assert first_messages[0] == {"role": "user", "content": [{"text": "Hello"}]}
    assert first_messages[1] == {"role": "assistant", "content": [{"text": "Hi, draw an area and ask me about it."}]}
    tool_names = {spec["name"] for spec in model.requests[0]["tool_specs"]}
    assert {"python_repl", "visualize_image", "visualize_map_raster_layer", "visualize_interactive_chart",
            "share_file_with_client"} <= tool_names
    # The geospatial library is documented in the system prompt by strands-code-agent.
    system_prompt = model.requests[0]["system_prompt"]
    for symbol in ("get_satellite_data", "compute_NDVI", "generate_overlay", "convert_lwir11_to_celsius"):
        assert symbol in system_prompt


def test_invocation_with_selected_model(client, monkeypatch):
    from geospatial_agent import agent
    from geospatial_agent.bedrock_models import CLAUDE_HAIKU_5_5_MODEL

    model_kwargs = {}

    def fake_bedrock_model(**kwargs):
        model_kwargs.update(kwargs)
        return ScriptedModel([[{"text": "Hello!"}]])

    monkeypatch.setattr(agent, "BedrockModel", fake_bedrock_model)

    with client.stream("POST", "/invocations", json={"message": "Hi", "model_id": CLAUDE_HAIKU_5_5_MODEL["id"]}) as response:
        events = read_sse_events(response)

    assert model_kwargs == {"model_id": CLAUDE_HAIKU_5_5_MODEL["id"]}
    assert [e["text"] for e in events if e.get("msg_type") == "text"] == ["Hello!"]
    assert events[-1]["msg_type"] == "result"


def test_model_config_sends_temperature_only_when_supported(monkeypatch):
    from geospatial_agent import agent
    from geospatial_agent.bedrock_models import DEFAULT_TEMPERATURE, MODELS

    # Every listed model declares whether it accepts `temperature`.
    for model_id, model in MODELS.items():
        assert isinstance(model.get("supports_temperature"), bool), model_id
        assert agent.model_config(model_id) == (
            {"model_id": model_id, "temperature": DEFAULT_TEMPERATURE} if model["supports_temperature"] else {"model_id": model_id})

    # Models accepting it (the default when unspecified) get the configured temperature.
    monkeypatch.setitem(MODELS, "test.model-with-temperature", {"id": "test.model-with-temperature", "cost": {}})
    assert agent.model_config("test.model-with-temperature") == {"model_id": "test.model-with-temperature", "temperature": DEFAULT_TEMPERATURE}
    assert agent.model_config("test.model-with-temperature", temperature=None) == {"model_id": "test.model-with-temperature"}


def test_invocation_reports_python_errors_to_the_model(client, monkeypatch):
    """Errors in the agent's code are returned to the LLM as REPL output, not as stream errors."""
    from geospatial_agent import agent

    model = ScriptedModel([
        [{"toolUse": {"name": "python_repl", "input": {"code": "get_satellite_data('sentinel', ['red', 'swir22'], AOI_COORDINATES, start_date=date(2024, 6, 1), end_date=date(2024, 6, 30))"}, "toolUseId": "t1"}}],
        [{"text": "The requested band is not available."}],
    ])
    monkeypatch.setattr(agent, "BedrockModel", lambda **_: model)

    with client.stream("POST", "/invocations", json={"message": "SWIR?", "coordinates": AOI_COORDINATES}) as response:
        events = read_sse_events(response)

    assert not [e for e in events if "error" in e], events
    output = next(e for e in events if e.get("msg_type") == "toolResult" and e["name"] == "python_repl")["content"][0]["text"]
    assert "No scenes contain all requested bands" in output
    assert events[-1]["msg_type"] == "result"


def test_invocation_reports_model_failure(client, monkeypatch):
    """A failing LLM call must surface as an SSE error event the UI can display."""
    from geospatial_agent import agent

    class FailingModel(ScriptedModel):
        async def stream(self, *args, **kwargs):
            raise RuntimeError("model unavailable")
            yield  # pragma: no cover

    monkeypatch.setattr(agent, "BedrockModel", lambda **_: FailingModel([]))

    with client.stream("POST", "/invocations", json={"message": "anything", "coordinates": AOI_COORDINATES}) as response:
        events = read_sse_events(response)

    assert any("error" in e for e in events), events


def test_sandbox_allows_the_geospatial_stack(client, monkeypatch):
    """The agent's code can import the geospatial libraries installed in the image."""
    from geospatial_agent import agent

    code = """
from shapely.geometry import Polygon, mapping
from shapely.ops import transform
import shapely
import pyproj
from pyproj import Transformer
import xarray as xr
import rioxarray
import rasterio
from rasterio.features import rasterize
import geopandas as gpd
import pystac
import pystac_client
import stackstac
from PIL import Image
aoi = Polygon(AOI_COORDINATES)
to_utm = Transformer.from_crs('EPSG:4326', 'EPSG:32630', always_xy=True)
area_km2 = transform(to_utm.transform, aoi).area / 1e6
gdf = gpd.GeoDataFrame(geometry=[aoi], crs='EPSG:4326')
print(f"AREA_KM2={area_km2:.2f} TYPE={mapping(aoi)['type']} CRS={gdf.crs.to_epsg()}")
"""
    model = ScriptedModel([
        [{"toolUse": {"name": "python_repl", "input": {"code": code}, "toolUseId": "t1"}}],
        [{"text": "Done."}],
    ])
    monkeypatch.setattr(agent, "BedrockModel", lambda **_: model)

    with client.stream("POST", "/invocations", json={"message": "Area?", "coordinates": AOI_COORDINATES}) as response:
        events = read_sse_events(response)

    output = next(e for e in events if e.get("msg_type") == "toolResult" and e["name"] == "python_repl")["content"][0]["text"]
    assert "STDERR" not in output, output
    assert "TYPE=Polygon CRS=4326" in output, output
    area = float(output.split("AREA_KM2=")[1].split()[0])
    assert 1.0 < area < 1.3, output  # ~1.39 km x 0.89 km


def test_visualizations_batched_with_the_code_producing_them(client, monkeypatch, tmp_path):
    """The model may call python_repl and the tools using its output files in the same message:
    the files are only read once the code has run. A missing file must not abort the stream."""
    from geospatial_agent import agent

    image_path = tmp_path / "ndvi.png"
    csv_path = tmp_path / "ndvi.csv"
    code = f"""
plt.figure(figsize=(2, 2))
plt.imshow(np.eye(4))
plt.savefig('{image_path}')
pd.DataFrame({{'ndvi': [0.5]}}).to_csv('{csv_path}', index=False)
print('SAVED')
"""
    model = ScriptedModel([
        [
            {"text": "Computing and showing the result."},
            {"toolUse": {"name": "python_repl", "input": {"code": code}, "toolUseId": "t1"}},
            {"toolUse": {"name": "visualize_image", "input": {"image_path": str(image_path)}, "toolUseId": "t2"}},
            {"toolUse": {"name": "visualize_map_raster_layer", "input": {"image_path": str(image_path), "folium_bounds": AOI_FOLIUM_BOUNDS}, "toolUseId": "t3"}},
            {"toolUse": {"name": "share_file_with_client", "input": {"file_path": str(csv_path)}, "toolUseId": "t4"}},
            {"toolUse": {"name": "visualize_image", "input": {"image_path": str(tmp_path / "never_written.png")}, "toolUseId": "t5"}},
        ],
        [{"text": "Done."}],
    ])
    monkeypatch.setattr(agent, "BedrockModel", lambda **_: model)

    with client.stream(
        "POST", "/invocations",
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": SESSION_ID},
        json={"message": "NDVI?", "coordinates": AOI_COORDINATES},
    ) as response:
        events = read_sse_events(response)

    assert not [e for e in events if "error" in e], events
    names = [(e.get("msg_type"), e.get("name"), e.get("toolUseId")) for e in events]

    # The code output is streamed before the visualizations it produced.
    repl_result = names.index(("toolResult", "python_repl", "t1"))
    for tool_use_id in ("t2", "t3", "t4"):
        assert names.index(("toolUse", next(n for _, n, i in names if i == tool_use_id), tool_use_id)) > repl_result

    by_id = {e["toolUseId"]: e for e in events if e.get("msg_type") == "toolUse"}
    decode_png(by_id["t2"]["image"])
    decode_png(by_id["t3"]["image"])
    assert by_id["t3"]["input"]["folium_bounds"] == AOI_FOLIUM_BOUNDS
    assert urlparse(by_id["t4"]["pre_signed_s3_url"]).path.endswith(f"{SESSION_ID}/ndvi.csv")
    assert ("toolResult", "share_file_with_client", "t4") in names

    # The missing image is reported as a warning, and the analysis continues.
    assert "t5" not in by_id
    warnings = [e["text"] for e in events if e.get("msg_type") == "text" and e["text"].startswith("⚠")]
    assert len(warnings) == 1 and "never_written.png" in warnings[0], warnings
    assert events[-1]["msg_type"] == "result"
