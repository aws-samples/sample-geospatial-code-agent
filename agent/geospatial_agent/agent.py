import os

import boto3
from strands.models import BedrockModel
from strands import tool

from jinja2 import Template

from strands_code_agent import CodeAgent
from strands_code_agent.utils import image_to_base64
from strands_code_agent.toolkits import Toolkit, VISUALIZATION_TOOLKIT, DATA_ANALYSIS_TOOLKIT

from geospatial_agent.bedrock_models import MODELS, DEFAULT_MODEL_ID, DEFAULT_TEMPERATURE
from geospatial import (
    get_satellite_data,
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
def share_file_with_client(file_path: str):
    """
    Share a big file with the client through S3.
    
    Args:
        file_path: the path of the file to be saved on S3 and shared with the client.
    """
    pass


# UI tools used to communicate with the client application, and not providing an actual response.
UI_TOOLS = {'visualize_image', 'visualize_map_raster_layer'}


class GeospatialAgent:
    def __init__(self, coordinates, session_id, history=None, model_id=DEFAULT_MODEL_ID) -> None:
        self.session_id = session_id
        self.cost = MODELS[model_id]['cost']
        self.tool_uses = {}

        messages = []
        if history is not None:
            for role, msg in history:
                messages.append({
                    'role': role,
                    'content': [{'text': msg}]
                })

        self.agent = CodeAgent(
            system_prompt=SYSTEM_PROMPT,
            tools=[visualize_image, visualize_map_raster_layer, share_file_with_client],
            toolkits=[
                VISUALIZATION_TOOLKIT, DATA_ANALYSIS_TOOLKIT,
                Toolkit(
                    initialization_code=INITIALIZATION_CODE.render(AOI_COORDINATES=coordinates),
                    domain_specific_code=[
                        get_satellite_data,
                        Index, ComputedIndex, compute_NDVI, compute_NDWI, compute_NBR, compute_dNBR,
                        generate_overlay,
                        convert_lwir11_to_celsius
                    ],
                ),
            ],
            model=BedrockModel(
                model_id=model_id,
                temperature=DEFAULT_TEMPERATURE,
            ),
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
                for content in event['message']['content']:
                    if 'text' in content:
                        yield {"msg_type": "text", "text": content['text']}

                    elif 'toolUse' in content:
                        toolUse = content['toolUse']
                        self.tool_uses[toolUse['toolUseId']] = toolUse['name']
                        toolUse["msg_type"] = "toolUse"

                        if toolUse['name'] in UI_TOOLS:
                            toolUse['image'] = image_to_base64(toolUse['input']['image_path'])

                        if toolUse['name'] == 'share_file_with_client':
                            file_key = f"{self.session_id}/{os.path.basename(toolUse['input']['file_path'])}"
                            with open(toolUse['input']['file_path'], 'rb') as f:
                                s3_client.put_object(Bucket=FILE_SHARING_BUCKET, Key=file_key, Body=f)
                            toolUse['pre_signed_s3_url'] = s3_client.generate_presigned_url(
                                'get_object',
                                Params={'Bucket': FILE_SHARING_BUCKET, 'Key': file_key},
                                ExpiresIn=PRESIGNED_URL_EXPIRATION
                            )

                        yield toolUse

                    elif 'toolResult' in content:
                        toolResult = content['toolResult']
                        toolResult['name'] = self.tool_uses[toolResult['toolUseId']]

                        if toolResult['name'] in UI_TOOLS:
                            continue

                        toolResult["msg_type"] = "toolResult"
                        yield toolResult
