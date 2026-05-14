# Geospatial Agent

An AI-powered geospatial analysis agent that enables natural language interaction with satellite imagery. Users draw a polygon on a map, ask a question (e.g., "What is the NDVI in this area?"), and the agent autonomously writes and executes Python code to fetch satellite data, run analyses, and return results — including images, statistics, and map overlays.

[![Demo Video](https://img.youtube.com/vi/Q5G0DdFtpTo/maxresdefault.jpg)](https://youtu.be/Q5G0DdFtpTo)

This is a [Code Agent](#the-coding-agent) providing better accuracy, higher speed, and lower cost compared to a traditional tool-invoking agent.

For a traditional tool-invoking Geospatial Agent solution, you can use: https://github.com/aws-samples/sample-geospatial-agent-on-aws

## Table of Contents

1. [Architecture](#architecture)
2. [Capabilities](#capabilities)
3. [The Coding Agent](#the-coding-agent)
4. [Geospatial Library](#geospatial-library)
5. [User Interface](#user-interface)
6. [Infrastructure](#infrastructure)
7. [Deployment](#deployment)
8. [Local Development](#local-development)
9. [Evaluation](#evaluation)
10. [Extending the Agent](#extending-the-agent)

## Architecture

| Component | Description | Directory |
|---|---|---|
| Coding Agent | Receives user questions, writes and executes Python code, returns results | `./agent/geospatial_agent/` |
| Geospatial Library | Satellite data retrieval, spectral index computation, thermal analysis | `./agent/geospatial_agent/geospatial/` |
| User Interface | React web app with chat panel and interactive map | `./user-interface/` |
| Infrastructure | AWS CDK stacks — VPC, AgentCore, Cognito, CloudFront | `./infrastructure/` |

The agent is hosted on **Amazon Bedrock AgentCore**, which manages the container lifecycle, scaling, and API endpoint. The web application is a **serverless static site** (S3 + CloudFront) with **Amazon Cognito** authentication.

## Capabilities

### Satellite Data Sources

| Satellite | Collection | Resolution | Notes |
|---|---|---|---|
| Sentinel-2 | `sentinel-2-l2a` | 10–20m | Visible + NIR/SWIR bands |
| Landsat | `landsat-c2-l2` | 30m | Includes thermal band (`lwir11`) |

Data is retrieved via STAC (SpatioTemporal Asset Catalog) from the Element84 Earth Search API and loaded as Cloud-Optimized GeoTIFFs (COGs).

### Spectral Index Analysis

| Index | Formula | Use Case |
|---|---|---|
| NDVI | (NIR − Red) / (NIR + Red) | Vegetation health and density |
| NDWI | (Green − NIR) / (Green + NIR) | Water body detection and moisture |
| NBR | (NIR − SWIR2) / (NIR + SWIR2) | Burn scar detection |
| dNBR | pre-fire NBR − post-fire NBR | Burn severity assessment |

Each index produces classified maps with statistics (mean, median, percentiles, class distribution) and transparent overlays rendered directly on the interactive map.

### Additional Analysis
- **Thermal Analysis** — Converts Landsat's `lwir11` band from Kelvin to Celsius for surface temperature analysis
- **Urban Heat Island Analysis** — Combines Landsat thermal data with NDVI vegetation indices to correlate surface temperature with vegetation coverage
- **Geocoding** — Converts place names to coordinates via Amazon Location Service
- **Report Generation** — Generates downloadable files (CSV, reports) via pre-signed S3 URLs

## The Coding Agent

### Coding Agent vs. Tool Agent

A critical architectural decision is the use of a **coding agent** rather than a traditional **tool-calling agent**.

**Tool-calling agents** define a fixed set of tools (functions) that the LLM can invoke. Each tool call is a single function invocation, and the LLM receives the full result back in its context window. This has several limitations:
- **Context flooding**: Large objects (e.g., satellite image arrays) must be serialized and sent back to the LLM, consuming context window space.
- **Sequential actions**: The LLM must make one tool call at a time, wait for the result, then decide the next step.
- **Rigid interfaces**: Every capability must be pre-defined as a tool with a fixed signature.

**Coding agents** give the LLM a Python interpreter instead. The LLM writes and executes Python scripts, which provides:
- **Efficient data handling**: Intermediate results stay in Python variables without flooding the LLM context.
- **Larger action plans**: Multi-step scripts perform complex analyses in a single execution.
- **No serialization overhead**: Data stays in Python memory — no need to serialize NumPy arrays to JSON and back.
- **Self-extending**: The agent can write helper functions on the fly if needed.

The agent is built with the [Strands Agents SDK](https://github.com/strands-agents/sdk-python) and the [`strands-code-agent`](https://pypi.org/project/strands-code-agent/) library, and has four tools:
- `python_repl` — Executes Python code (primary tool)
- `visualize_image` — Sends a PNG image to the UI
- `visualize_map_raster_layer` — Adds a raster overlay to the map
- `share_file_with_client` — Uploads a file to S3 and shares a pre-signed URL

The `visualize_image` and `visualize_map_raster_layer` tools are "UI tools" — they don't return a meaningful result to the LLM. Instead, they trigger the user interface to display an image or add a map layer.

### AgentCore

Amazon Bedrock AgentCore is a managed service that hosts and runs AI agents. You provide a Docker image containing your agent code, and AgentCore takes care of running it, exposing an API endpoint, managing sessions, and handling scaling.

The agent container is built from `./agent/` and pushed to ECR during deployment. The entry point (`./agent/geospatial_agent/agent_service.py`) receives a JSON payload containing the user's message, polygon coordinates, and conversation history. The agent streams back events (text, tool calls, images, results) as server-sent events.

### The Python Environment

The Python interpreter (provided by `strands-code-agent`) defaults to a sandboxed environment:
- **State persistence within a turn**: Variables persist across multiple `python_repl` calls within a single user message — the agent can fetch data in one code block, then analyze it in the next.
- **State reset between turns**: The interpreter resets completely with each new user message.
- **Pre-loaded environment**: The geospatial library functions, NumPy, datetime utilities, and matplotlib are pre-loaded — the agent does not need to import them.

### Code Documentation

The `strands-code-agent` library automatically extracts documentation from Python functions and classes using `inspect`. This documentation is injected into the agent's system prompt so the LLM knows what functions are available and how to use them. When you add a new function to the geospatial library with a proper docstring and type hints, it is automatically available to the agent.

### Supported LLM Models

The agent supports multiple foundation models through Amazon Bedrock, configured in `./agent/geospatial_agent/bedrock_models.py`. Each model entry includes per-token cost information used to calculate the on-demand cost of each agent invocation, displayed to the user after each response.

## Geospatial Library

The geospatial library (`./agent/geospatial_agent/geospatial/`) is the heart of the agent's capabilities. The agent's LLM does not perform geospatial analysis itself — it writes Python code that calls functions from this library.

**To expand the agent's capabilities, you expand this library.**

### Satellite Data Retrieval

**File**: `./agent/geospatial_agent/geospatial/satellite_data.py`

This module retrieves satellite imagery from Sentinel-2 and Landsat using the STAC protocol and Cloud-Optimized GeoTIFFs:

1. **Scene Search** (`search_satellite_scenes`): Queries the Element84 Earth Search STAC server for scenes intersecting the user's polygon, within the specified date range and cloud cover threshold. Results are grouped by grid cell (e.g., MGRS tiles for Sentinel-2).
2. **Scene Selection** (`select_best_scene`): Selects the scene with the best coverage and lowest cloud cover — finds the grid cell with the largest overlap with the AOI, then picks the scene with the lowest cloud coverage.
3. **Band Fetching** (`fetch_scene_bands`): Uses `stackstac` to load spectral bands from COG files on S3, clipped to the polygon's bounding box, into an `xarray.DataArray`. Band names are normalized across satellites (e.g., Landsat's `nir08` → `nir`).
4. **Main Entry Point** (`get_satellite_data`): Orchestrates the full workflow and returns scene metadata plus the loaded data array.

The module also includes a `geocode` function using Amazon Location Service to convert place names to coordinates.

**Key concepts:**
- **STAC**: An open standard for describing geospatial data — a search engine for satellite imagery.
- **COG**: Cloud-Optimized GeoTIFF — allows reading only a portion of an image without downloading the entire file.
- **Spectral Bands**: Satellites capture light at different wavelengths (`red`, `green`, `blue` for visible; `nir` for near-infrared reflected by vegetation; `swir` for shortwave infrared useful for moisture and burn scars).

### Spectral Index Analysis

**File**: `./agent/geospatial_agent/geospatial/index_analysis.py`

Spectral indices are mathematical combinations of satellite bands that highlight specific surface properties. The module defines an extensible framework built around two classes:

- **`Index`** — Defines an index's metadata: name, required bands, valid range, colormap, and classification thresholds.
- **`ComputedIndex`** — Stores computed values and provides `get_statistics()` (min, max, mean, median, std, percentiles), `get_class_percentages()` (pixel distribution per class), and `class_to_rgba()` (RGBA image for visualization).

### Additional Modules

- **Thermal Analysis** (`thermal.py`): Converts Landsat's `lwir11` thermal band from Kelvin to Celsius for surface temperature analysis.
- **Visualization** (`visualization.py`): `generate_overlay` creates transparent PNG images from analysis data (e.g., NDVI maps) that can be overlaid on the web map.
- **Data Manipulation** (`data_manipulation.py`): Utility functions for safe division (handling division by zero), band extraction from xarray DataArrays, and array classification.

## User Interface

The user interface is a **serverless React web application** that runs entirely in the browser. It connects to Amazon Cognito for authentication and to the AgentCore API for agent interactions. There is no backend server — it is a static site served from S3 through CloudFront.

**Directory**: `./user-interface/src/`

The application uses the [Cloudscape Design System](https://cloudscape.design/) and includes:

- **Interactive Map** (`MapView.tsx`): Built with MapLibre GL JS. Supports multiple base maps (CARTO Dark, Google Roads, Google Satellite, Esri Satellite), polygon drawing, layer management (toggle visibility, zoom to, remove), and image overlays for raster analysis results at correct geographic positions.
- **Chat Interface** (`ChatSidebar.tsx`): Markdown rendering (GitHub Flavored Markdown), syntax-highlighted code blocks, inline image display, polygon input (draw or paste JSON), conversation history, print/export, and session management.
- **Authentication** (`auth/`): Amazon Cognito via AWS Amplify — login, first-login password change, and scoped IAM credentials for AgentCore invocation.
- **AgentCore Streaming** (`services/api.ts`): Async generator that yields typed events (`text`, `python_code`, `execution_output`, `image`, `file_link`, `result`, `error`) from the server-sent event stream. Supports both remote (AgentCore) and local (`localhost:8080`) modes.

## Infrastructure

All infrastructure is defined in **AWS CDK** (Python). Three stacks deployed in order:

```
VPCStack → AgentCoreStack → WebAppStack
```

All stacks are validated against **cdk-nag** (AWS Solutions checks).

### VPC Stack

**File**: `./infrastructure/stacks/vpc_stack.py`

- 2 Availability Zones for high availability
- Public subnets with NAT Gateway for outbound internet access
- Private isolated subnets for agent containers (no direct internet access — outbound via NAT Gateway)
- VPC Flow Logs to CloudWatch for network traffic monitoring

### AgentCore Stack

**File**: `./infrastructure/stacks/agentcore_stack.py`

- **Container Build Pipeline**: ECR repository, CodeBuild project, Lambda-backed Custom Resource that triggers builds on code changes (tracked via source asset hash)
- **S3 Buckets**: `client-file-sharing` for temporary file sharing with users (7-day expiration), plus access logs bucket
- **Amazon Location Service**: Place Index (Esri) for geocoding
- **AgentCore Runtime**: `CfnRuntime` resource with container image, VPC configuration, IAM execution role, and environment variables
- **IAM Role**: Permissions for Bedrock model invocation, S3 read/write, Location Service, CloudWatch Logs, and X-Ray

### Web Application Stack

**File**: `./infrastructure/stacks/webapp_stack.py`

- **Amazon Cognito**: User Pool (password policy), User Pool Client (no client secret), Identity Pool mapping authenticated users to IAM roles
- **IAM for Authenticated Users**: Single permission — `bedrock-agentcore:InvokeAgentRuntime` scoped to the specific agent runtime ARN
- **Static Hosting**: S3 bucket + CloudFront distribution with Origin Access Control, HTTPS with TLS 1.2, SPA routing (404/403 → `index.html`)

## Deployment

```bash
./scripts/deploy.sh --cdk
```

This will:
1. Deploy all CDK stacks (VPC → AgentCore → WebApp)
2. Retrieve CloudFormation outputs (Cognito IDs, bucket names, etc.)
3. Generate the `.env` file for the React app
4. Build the React application (`npm run build`)
5. Upload the build to S3
6. Invalidate the CloudFront cache

To skip CDK deployment (e.g., UI-only changes):
```bash
./scripts/deploy.sh
```

### Post-Deployment: Create a User

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <user-pool-id> \
  --username <username> \
  --temporary-password <password>
```

The user will be prompted to change their password on first login.

## Local Development

### Run the Agent Locally
```bash
python -m geospatial_agent.agent_service
```
Starts the agent on `http://localhost:8080`. Requires AWS credentials configured for Bedrock, S3, etc.

### Run the UI Locally

Connecting to the remote AgentCore endpoint:
```bash
./scripts/start-ui-local.sh
```

Connecting to a local agent on localhost:
```bash
./scripts/start-ui-local.sh --local
```

The script retrieves configuration from CloudFormation, generates `.env.local`, and starts the dev server. Infrastructure must be deployed first.

## Evaluation

The evaluation framework in `./evaluation/` benchmarks the agent against predefined test cases in `./data/tests.json`.

```bash
cd evaluation
python benchmark_agent.py                    # Run all tests
python benchmark_agent.py --use_case "NDVI"  # Run specific use case
```

Each test is defined as:
```json
{
    "id": "1",
    "use-case": "NDVI",
    "question": "What is the NDVI in this area?",
    "answer": "0.45",
    "aoi": [[lon1, lat1], [lon2, lat2], ...]
}
```

### LLM-as-Judge

Responses are scored using an **LLM-as-Judge** (Claude Haiku 4.5, temperature 0.2) that compares generated answers against expected answers, returning a 0–1 score with rationale. For numerical answers, the score degrades proportionally to the percentage error. Results are cached on disk to avoid re-evaluating identical pairs.

The `AgentClient` class in `./evaluation/agent_client.py` handles Cognito authentication and AgentCore invocation, mirroring what the web UI does.

## Extending the Agent

### Adding New Geospatial Capabilities

1. Add functions (with docstrings and type hints) to `./agent/geospatial_agent/geospatial/`
2. Export in `__init__.py` and register in the `IMPORTED_CODE` list in `agent.py`
3. Redeploy — CDK detects code changes and rebuilds the container automatically

The agent auto-discovers function documentation via `strands-code-agent`, so new functions are immediately available to the LLM.

### Adding a New Spectral Index

1. Define an `Index` dataclass with metadata (bands, valid range, colormap, classification thresholds)
2. Write a `compute_*` function with docstring and type hints
3. Export it in `./agent/geospatial_agent/geospatial/__init__.py`
4. Register it in the `IMPORTED_CODE` list in `./agent/geospatial_agent/agent.py`

```python
MY_INDEX = Index(
    name="My Custom Index",
    bands=["band1", "band2"],
    valid_range=(-1.0, 1.0),
    index_cmap='RdYlGn',
    classes=[
        ("Low",    (-1.0, 0.0), "#FF0000"),
        ("Medium", ( 0.0, 0.5), "#FFFF00"),
        ("High",   ( 0.5, 1.0), "#00FF00"),
    ]
)

def compute_my_index(data: xr.DataArray) -> ComputedIndex:
    """Compute My Custom Index. MY_INDEX = (band1 - band2) / (band1 + band2)"""
    bands = get_bands(data, MY_INDEX.bands)
    return ComputedIndex(
        MY_INDEX,
        safe_divide(bands['band1'] - bands['band2'], bands['band1'] + bands['band2'])
    )
```

### Adding New Satellite Sources

Add an entry to the `SATELLITES` dictionary in `satellite_data.py` with the STAC collection name, start date, and band name mappings. If the new satellite uses a different STAC server, you may need to modify `search_satellite_scenes` to support multiple servers.

### Changing the LLM Model

Edit `./agent/geospatial_agent/bedrock_models.py`. The `DEFAULT_MODEL_ID` variable controls which model is used. Each model entry includes per-token cost info for the metrics display.

## Authors
* Emilio Monti
* Ozan Cihangir
* Luis Orus

## Security

### Network Access Control

The agent requires outbound internet access to retrieve satellite imagery from external sources such as **Element84 Earth Search STAC API** (`earth-search.aws.element84.com`).

To prevent the agent from accessing unintended public domains, we recommend network level restriction of egress traffic to only the required endpoints. [AWS Network Firewall](https://docs.aws.amazon.com/network-firewall/latest/developerguide/what-is-aws-network-firewall.html) supports domain-based filtering with stateful rules, allowing you to create an HTTPS allow-list that restricts the agent's outbound traffic at the network level.

### Reporting security issues

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.