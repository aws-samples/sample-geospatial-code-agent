import os

import boto3
from strands.models import BedrockModel
from strands import tool

from jinja2 import Template

from strands_code_agent import CodeAgent
from strands_code_agent.utils import image_to_base64
from strands_code_agent.toolkits import Toolkit, VISUALIZATION_TOOLKIT, DATA_ANALYSIS_TOOLKIT

PLOTLY_TOOLKIT = Toolkit(
    libraries=['plotly.*'],
    initialization_code="import plotly.express as px\nimport plotly.graph_objects as go",
    usage_instructions="""
When creating interactive charts, use plotly.express or plotly.graph_objects.
Save the figure to a JSON file with fig.write_json(), then call the visualize_interactive_chart tool with the file path.
Do NOT call fig.show() — there is no GUI.
""",
)

from geospatial_agent.bedrock_models import MODELS, DEFAULT_MODEL_ID, DEFAULT_TEMPERATURE
from geospatial import (
    get_satellite_data, get_high_resolution_image, detect_objects,
    Index, ComputedIndex, compute_NDVI, compute_NDWI, compute_NBR, compute_dNBR,
    generate_overlay,
    convert_lwir11_to_celsius
)

FILE_SHARING_BUCKET = os.environ.get('CLIENT_FILE_SHARING_BUCKET_NAME', 'client-file-sharing-430302394720-us-east-1')
PRESIGNED_URL_EXPIRATION = 3600  # 1 hour

s3_client = boto3.client('s3')


SYSTEM_PROMPT = """
You are an expert geospatial analyst proficient in python and its geospatial analysis libraries.
You can use the python_repl tool to execute python code to fetch and analyse images from Landsat and Sentinel-2.
"""

INITIALIZATION_CODE = Template("""
# Area Of Interest coordinates for the analysis
AOI_COORDINATES = {{AOI_COORDINATES}}
""")


@tool
def visualize_image(image_path: str):
    """
    Load the given PNG image at `image_path` and visualize it on the user client application.
    
    Args:
        image_path: the path of the PNG image to be visualized.
    """
    pass


@tool
def visualize_map_raster_layer(
        image_path: str,
        folium_bounds: list[list[float]]):
    """
    Load a raster PNG from `image_path` and add it as an image overlay to
    the client folium map at the given `folium_bounds`.
    
    Args:
        image_path: the path of the PNG image to be visualized.
        folium_bounds: The bounds of the layer in folium format. [[south, west], [north, east]]
    """
    pass


@tool
def visualize_interactive_chart(chart_path: str, chart_type: str = "plotly"):
    """
    Send an interactive chart to the client for rendering.
    The chart will be rendered as an interactive visualization with zoom, pan, and hover tooltips.

    First save the chart JSON to a file, then call this tool with the file path:
        fig = px.line(df, x='date', y='ndvi_mean')
        fig.write_json('/tmp/.../chart.json')
        visualize_interactive_chart(chart_path='/tmp/.../chart.json')

    Args:
        chart_path: Path to a JSON file containing the chart specification (written with fig.write_json() for Plotly).
        chart_type: Either "plotly" or "vega-lite".
    """
    pass


@tool
def share_file_with_client(file_path: str):
    """
    Share a big file with the client through S3.
    
    Args:
        file_path: the path of the file to be saved on S3 and shared with the client.
    """
    pass


# UI tools used to communicate with the client application, and not providing an actual response.
UI_TOOLS = {'visualize_image', 'visualize_map_raster_layer', 'visualize_interactive_chart'}
# Tools whose input is a file written by the agent's code (read when streaming the event to the client).
FILE_TOOLS = UI_TOOLS | {'share_file_with_client'}

# Geospatial libraries installed in the agent image that the agent's code may import.
GEOSPATIAL_LIBRARIES = [
    'shapely.*', 'pyproj.*', 'geopandas.*',
    'xarray.*', 'rioxarray.*', 'rasterio.*', 'rio_tiler.*',
    'pystac.*', 'pystac_client.*', 'stackstac.*',
]


def model_config(model_id, temperature=DEFAULT_TEMPERATURE):
    """BedrockModel arguments for `model_id`.

    Some models (e.g. Claude 5.5) reject `temperature`: only send it when supported.
    """
    config = {'model_id': model_id}
    if MODELS[model_id].get('supports_temperature', True) and temperature is not None:
        config['temperature'] = temperature
    return config


class GeospatialAgent:
    def __init__(self, coordinates, session_id, history=None, model_id=DEFAULT_MODEL_ID) -> None:
        self.session_id = session_id
        self.cost = MODELS[model_id]['cost']
        self.tool_uses = {}
        self.pending_file_tools = {}

        messages = []
        if history is not None:
            for role, msg in history:
                messages.append({
                    'role': role,
                    'content': [{'text': msg}]
                })

        self.agent = CodeAgent(
            system_prompt=SYSTEM_PROMPT,
            tools=[visualize_image, visualize_map_raster_layer, visualize_interactive_chart, share_file_with_client],
            toolkits=[
                VISUALIZATION_TOOLKIT, DATA_ANALYSIS_TOOLKIT, PLOTLY_TOOLKIT,
                Toolkit(
                    libraries=['PIL.*', 'pycocotools.*', *GEOSPATIAL_LIBRARIES],
                    initialization_code=INITIALIZATION_CODE.render(AOI_COORDINATES=coordinates),
                    domain_specific_code=[
                        get_satellite_data, get_high_resolution_image, detect_objects,
                        Index, ComputedIndex, compute_NDVI, compute_NDWI, compute_NBR, compute_dNBR,
                        generate_overlay,
                        convert_lwir11_to_celsius
                    ],
                ),
            ],
            model=BedrockModel(**model_config(model_id)),
            callback_handler=None,
            messages=messages,
        )

    def __post_process_result(self, response):
        metrics = response.metrics.get_summary()
        return {
            'response': str(response),
            'metrics': {
                'agent': {
                    'total_cycles': metrics['total_cycles'],
                    'total_duration': metrics['total_duration'],
                    'on_demand_cost': (metrics['accumulated_usage']['inputTokens'] * self.cost['on_demand']['input']
                                     + metrics['accumulated_usage']['outputTokens'] * self.cost['on_demand']['output'])
                }
            }
        }

    async def stream_async(self, user_message):
        async for event in self.agent.stream_async(user_message):
            if 'result' in event:
                yield {
                    'msg_type': 'result',
                    'result': self.__post_process_result(event['result'])
                }
            elif 'message' in event:
                # Tools reading files are streamed after the other tool results of the same
                # batch (e.g. the output of the code that wrote those files).
                ready = []
                for content in event['message']['content']:
                    if 'text' in content:
                        yield {"msg_type": "text", "text": content['text']}

                    elif 'toolUse' in content:
                        toolUse = content['toolUse']
                        self.tool_uses[toolUse['toolUseId']] = toolUse['name']
                        toolUse["msg_type"] = "toolUse"

                        if toolUse['name'] in FILE_TOOLS:
                            # The model may batch the code writing the file and the tool using it
                            # in the same message: defer reading the file until the tools have run.
                            self.pending_file_tools[toolUse['toolUseId']] = toolUse
                            continue

                        yield toolUse

                    elif 'toolResult' in content:
                        toolResult = content['toolResult']
                        toolResult['name'] = self.tool_uses[toolResult['toolUseId']]
                        toolResult["msg_type"] = "toolResult"

                        if toolResult['name'] in FILE_TOOLS:
                            toolUse = self.pending_file_tools.pop(toolResult['toolUseId'], None)
                            if toolUse is not None:
                                # The results of UI tools are not meaningful: don't stream them.
                                ready.append((toolUse, None if toolResult['name'] in UI_TOOLS else toolResult))
                            continue

                        yield toolResult

                # The tool-result message is emitted after every tool of the batch has run,
                # so the files written by the code are now available.
                for toolUse, toolResult in ready:
                    yield self.__load_file(toolUse)
                    if toolResult is not None:
                        yield toolResult

    def __load_file(self, toolUse):
        """Attach the image / chart spec / shared file URL to a toolUse event, once its file exists."""
        try:
            name = toolUse['name']
            if name == 'visualize_interactive_chart':
                with open(toolUse['input']['chart_path'], 'r') as f:
                    toolUse['chart_spec'] = f.read()
            elif name == 'share_file_with_client':
                file_path = toolUse['input']['file_path']
                file_key = f"{self.session_id}/{os.path.basename(file_path)}"
                with open(file_path, 'rb') as f:
                    s3_client.put_object(Bucket=FILE_SHARING_BUCKET, Key=file_key, Body=f)
                toolUse['pre_signed_s3_url'] = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': FILE_SHARING_BUCKET, 'Key': file_key},
                    ExpiresIn=PRESIGNED_URL_EXPIRATION
                )
            else:
                toolUse['image'] = image_to_base64(toolUse['input']['image_path'])
        except (OSError, KeyError) as e:
            # Don't abort the whole analysis because one file is missing.
            return {"msg_type": "text", "text": f"⚠ Could not load the output of {toolUse['name']}: {e}"}
        return toolUse
