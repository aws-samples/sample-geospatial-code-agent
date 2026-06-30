#!/usr/bin/env python3
import os
import aws_cdk as cdk
import cdk_nag

from stacks.vpc_stack import VPCStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.webapp_stack import WebAppStack
from stacks.falcon_perception_stack import FalconPerceptionStack

env = cdk.Environment(
    account=os.environ.get("AWS_ACCOUNT") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("AWS_REGION") or os.environ.get("CDK_DEFAULT_REGION")
)

app = cdk.App()

vpc_stack = VPCStack(app, "GeospatialAgentVPCStack", env=env)

agentcore_stack = AgentCoreStack(app, "GeospatialAgentCoreStack", vpc=vpc_stack.vpc, env=env)

# Optional: Object detection model endpoint (GPU instance — adds cost)
deploy_object_detection = app.node.try_get_context("deploy_object_detection") or False
if deploy_object_detection in ("true", "True", True):
    falcon_stack = FalconPerceptionStack(app, "FalconPerceptionStack", vpc=vpc_stack.vpc, env=env)

webapp_stack = WebAppStack(app, "GeospatialWebAppStack", agent_stack=agentcore_stack, env=env)

cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks())
app.synth()
