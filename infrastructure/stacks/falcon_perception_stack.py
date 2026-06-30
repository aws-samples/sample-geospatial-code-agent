from aws_cdk import (
    Stack, CfnOutput,
    aws_ecr as ecr, aws_iam as iam, aws_sagemaker as sagemaker,
    aws_ec2 as ec2,
)
from constructs import Construct
from cdk_nag import NagSuppressions


class FalconPerceptionStack(Stack):
    """SageMaker endpoint for Falcon-Perception object detection.

    Prerequisites:
        Run ./scripts/build-falcon-perception.sh to build and push
        the container image to ECR before deploying this stack.
    """

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

        # Endpoint config
        # ml.g5.xlarge: A10G GPU, 24GB VRAM. If cu128 fails due to driver version,
        # request quota for ml.g6.xlarge (L4, driver >= 570, guaranteed cu128 support).
        endpoint_config = sagemaker.CfnEndpointConfig(self, "EndpointConfig",
            endpoint_config_name="falcon-perception-endpoint-config",
            production_variants=[sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="primary",
                model_name=model.model_name,
                initial_instance_count=1,
                instance_type="ml.g5.xlarge",
                initial_variant_weight=1,
            )]
        )
        endpoint_config.add_dependency(model)

        # Endpoint
        self.endpoint_name = "falcon-perception-object-detection"
        endpoint = sagemaker.CfnEndpoint(self, "Endpoint",
            endpoint_name=self.endpoint_name,
            endpoint_config_name=endpoint_config.endpoint_config_name,
        )
        endpoint.add_dependency(endpoint_config)

        CfnOutput(self, "EndpointName", value=self.endpoint_name)

        # Nag suppressions
        NagSuppressions.add_resource_suppressions(sm_role,
            [
                {"id": "AwsSolutions-IAM4", "reason": "SageMaker managed policy required for endpoint operation"},
                {"id": "AwsSolutions-IAM5", "reason": "ECR access requires wildcards"}
            ], apply_to_children=True)
