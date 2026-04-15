from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

from geospatial_agent.bedrock_models import DEFAULT_MODEL_ID

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload):
    # Keep the import within the scope of the invocation function to be able to
    # catch exceptions about missing packages, and log them on CloudWatch
    from geospatial_agent.agent import GeospatialAgent

    user_message = payload['message']
    coordinates = payload.get('coordinates')
    history = payload.get('history')
    model_id = payload.get('model_id', DEFAULT_MODEL_ID)
    session_id = BedrockAgentCoreContext.get_session_id()
    agent = GeospatialAgent(coordinates, session_id, history, model_id)
    async for msg in agent.stream_async(user_message):
        yield msg


if __name__ == "__main__":
    """
    To spin-up the Agent server locally run:
    python -m geospatial_agent.agent_service
    """
    app.run()
