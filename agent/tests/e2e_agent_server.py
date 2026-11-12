"""Run the real agent service on http://127.0.0.1:8080, fully offline, for UI end-to-end tests.

AWS is emulated by `offline_env` (moto + botocore hooks), satellite imagery comes from
its local STAC API, and the Bedrock LLM is replaced by `ScriptedModel` replaying
`analysis_scenario` on every request, so the web UI receives exactly the SSE stream
the production service would emit.

Usage (from the `agent` directory):
    python tests/e2e_agent_server.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import offline_env  # noqa: E402  (must be imported before geospatial_agent)

from geospatial_agent import agent  # noqa: E402
from geospatial_agent.agent_service import app  # noqa: E402


def scripted_bedrock_model(**_):
    work_dir = tempfile.mkdtemp(prefix="e2e-agent-")
    turns, _code = offline_env.analysis_scenario(work_dir)
    return offline_env.ScriptedModel(turns)


agent.BedrockModel = scripted_bedrock_model


if __name__ == "__main__":
    app.run(port=int(os.environ.get("E2E_AGENT_PORT", "8080")), host="127.0.0.1")
