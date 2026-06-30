from aws_cdk import (
    Stack, CfnOutput,
    aws_ecr as ecr, aws_iam as iam, aws_sagemaker as sagemaker,
    aws_ec2 as ec2,
)
from constructs import Construct
from cdk_nag import NagSuppressions


class FalconPerceptionStack(Stack):
    """SageMaker endpoint for Falcon-Perception object detection.

    Uses Inference Components with ManagedInstanceScaling (min=0) to enable
    scale-to-zero. Use the start/stop scripts to control the endpoint:
        ./scripts/falcon-perception-start.sh  (sets desired copies to 1)
        ./scripts/falcon-perception-stop.sh   (sets desired copies to 0)

    Prerequisites:
        Run ./scripts/build-falcon-perception.sh to build and push
        the container image to ECR before deploying this stack.
    """

    ENDPOINT_NAME = "falcon-perception-object-detection"
    INFERENCE_COMPONENT_NAME = "falcon-perception-model"

    def __init__(self, scope: Construct, id: str, vpc: ec2.IVpc, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Reference the pre-built ECR repository
        repo = ecr.Repository.from_repository_name(self, "Repo", "falcon-perception-inference")

        # SageMaker execution role
        sm_role = iam.Role(self, "SageMakerRole", assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"))
        sm_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"))
        repo.grant_pull(sm_role)

        # Security group
        endpoint_sg = ec2.SecurityGroup(self, "EndpointSG", vpc=vpc, description="Falcon-Perception endpoint SG")

        # SageMaker Model (weights downloaded at container startup from HuggingFace)
        model = sagemaker.CfnModel(self, "Model",
            model_name="falcon-perception",
            execution_role_arn=sm_role.role_arn,
            primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(
                image=repo.repository_uri_for_tag("latest"),
            ),
            vpc_config=sagemaker.CfnModel.VpcConfigProperty(
                security_group_ids=[endpoint_sg.security_group_id],
                subnets=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnet_ids
            )
        )

        # Endpoint config — no model_name on the variant (model is on the InferenceComponent).
        # ManagedInstanceScaling with min=0 enables scale-to-zero.
        # ml.g6.xlarge: L4 GPU, 24GB VRAM, Ada Lovelace architecture.
        endpoint_config = sagemaker.CfnEndpointConfig(self, "EndpointConfig",
            endpoint_config_name="falcon-perception-endpoint-config",
            execution_role_arn=sm_role.role_arn,
            vpc_config=sagemaker.CfnEndpointConfig.VpcConfigProperty(
                security_group_ids=[endpoint_sg.security_group_id],
                subnets=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnet_ids,
            ),
            production_variants=[sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="primary",
                instance_type="ml.g6.xlarge",
                initial_instance_count=1,
                managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                    min_instance_count=0,
                    max_instance_count=1,
                    status="ENABLED",
                ),
                routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                    routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                ),
                # Container takes ~5-8 min to start (model download + torch.compile + CUDA graphs)
                container_startup_health_check_timeout_in_seconds=600,
                model_data_download_timeout_in_seconds=600,
            )]
        )
        # No dependency on model here — model is referenced by the InferenceComponent
        endpoint_config.node.add_dependency(sm_role)

        # Endpoint
        self.endpoint_name = self.ENDPOINT_NAME
        endpoint = sagemaker.CfnEndpoint(self, "Endpoint",
            endpoint_name=self.endpoint_name,
            endpoint_config_name=endpoint_config.endpoint_config_name,
        )
        endpoint.add_dependency(endpoint_config)

        # Inference Component — associates the model with the endpoint and defines
        # resource requirements. copy_count=1 starts with one active copy.
        # Use UpdateInferenceComponentRuntimeConfig to set desired_copy_count=0 (stop)
        # or desired_copy_count=1 (start).
        inference_component = sagemaker.CfnInferenceComponent(self, "InferenceComponent",
            endpoint_name=self.endpoint_name,
            inference_component_name=self.INFERENCE_COMPONENT_NAME,
            variant_name="primary",
            specification=sagemaker.CfnInferenceComponent.InferenceComponentSpecificationProperty(
                model_name=model.model_name,
                compute_resource_requirements=sagemaker.CfnInferenceComponent.InferenceComponentComputeResourceRequirementsProperty(
                    number_of_accelerator_devices_required=1,
                    min_memory_required_in_mb=512,  # Must be 300-1024 on 16 GiB instances (g5/g6.xlarge)
                ),
                startup_parameters=sagemaker.CfnInferenceComponent.InferenceComponentStartupParametersProperty(
                    container_startup_health_check_timeout_in_seconds=600,
                    model_data_download_timeout_in_seconds=600,
                ),
            ),
            runtime_config=sagemaker.CfnInferenceComponent.InferenceComponentRuntimeConfigProperty(
                copy_count=1,
            ),
        )
        inference_component.add_dependency(endpoint)
        inference_component.add_dependency(model)

        CfnOutput(self, "EndpointName", value=self.endpoint_name)
        CfnOutput(self, "InferenceComponentName", value=self.INFERENCE_COMPONENT_NAME)

        # Nag suppressions
        NagSuppressions.add_resource_suppressions(sm_role,
            [
                {"id": "AwsSolutions-IAM4", "reason": "SageMaker managed policy required for endpoint operation"},
                {"id": "AwsSolutions-IAM5", "reason": "ECR access requires wildcards"}
            ], apply_to_children=True)
