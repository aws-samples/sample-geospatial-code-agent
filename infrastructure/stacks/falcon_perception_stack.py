import hashlib

from aws_cdk import (
    Stack, CfnOutput, Duration, CustomResource,
    aws_ecr as ecr, aws_iam as iam, aws_sagemaker as sagemaker,
    aws_ec2 as ec2, aws_codebuild as codebuild, aws_s3_assets as s3_assets,
    aws_lambda as _lambda, aws_kms as kms,
)
from constructs import Construct
from cdk_nag import NagSuppressions


class FalconPerceptionStack(Stack):
    """SageMaker endpoint for Falcon-Perception object detection.

    Includes a CodeBuild pipeline that builds the container image automatically
    when the source changes (Dockerfile, handler.py, buildspec.yml).

    Uses Inference Components with ManagedInstanceScaling (min=0) to enable
    scale-to-zero. Use the start/stop scripts to control the endpoint:
        ./scripts/falcon-perception-start.sh  (sets desired copies to 1)
        ./scripts/falcon-perception-stop.sh   (sets desired copies to 0)
    """

    ENDPOINT_NAME = "falcon-perception-object-detection"
    INFERENCE_COMPONENT_NAME = "falcon-perception-model"
    ECR_REPO_NAME = "falcon-perception-inference"

    def __init__(self, scope: Construct, id: str, vpc: ec2.IVpc, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ===== Container Image Build Pipeline =====

        # ECR repository — look up existing repo (created on first deploy via build script or prior stack)
        # The repo persists across stack deletions to preserve built images.
        repo = ecr.Repository.from_repository_name(self, "Repo", self.ECR_REPO_NAME)

        # Source asset (Dockerfile + handler.py + buildspec.yml)
        source_asset = s3_assets.Asset(self, "SourceAsset",
            path="sagemaker/falcon-perception"
        )

        # CodeBuild IAM role
        codebuild_role = iam.Role(self, "CodeBuildRole",
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
            inline_policies={
                "CodeBuildPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="CloudWatchLogs",
                            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/codebuild/*"],
                        ),
                        iam.PolicyStatement(
                            sid="ECRRepoAccess",
                            actions=[
                                "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer",
                                "ecr:BatchGetImage", "ecr:PutImage",
                                "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
                            ],
                            resources=[repo.repository_arn],
                        ),
                        iam.PolicyStatement(
                            sid="ECRAuthorization",
                            actions=["ecr:GetAuthorizationToken"],
                            resources=["*"],
                        ),
                        iam.PolicyStatement(
                            sid="S3SourceAccess",
                            actions=["s3:GetObject"],
                            resources=[f"{source_asset.bucket.bucket_arn}/*"],
                        ),
                    ]
                )
            },
        )

        NagSuppressions.add_resource_suppressions(codebuild_role,
            [{"id": "AwsSolutions-IAM5", "reason": "CodeBuild requires wildcards for CloudWatch logs, ECR auth token, and S3 source assets"}],
            apply_to_children=True
        )

        codebuild_encryption_key = kms.Key(self, "CodeBuildEncryptionKey",
            enable_key_rotation=True,
            description="KMS key for Falcon-Perception CodeBuild project encryption"
        )

        # CodeBuild project — x86_64 (GPU instances are x86), LARGE for Docker builds
        build_project = codebuild.Project(self, "ImageBuildProject",
            project_name=f"falcon-perception-build-{self.region}",
            description="Build Falcon-Perception inference container image",
            role=codebuild_role,
            timeout=Duration.minutes(30),
            encryption_key=codebuild_encryption_key,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,
            ),
            source=codebuild.Source.s3(
                bucket=source_asset.bucket, path=source_asset.s3_object_key
            ),
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "pre_build": {
                        "commands": [
                            "echo Logging in to Amazon ECR...",
                            "aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com",
                        ]
                    },
                    "build": {
                        "commands": [
                            "echo Build started on `date`",
                            "echo Building Falcon-Perception inference image...",
                            "docker build -t $IMAGE_REPO_NAME:latest .",
                            "docker tag $IMAGE_REPO_NAME:latest $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$IMAGE_REPO_NAME:latest",
                        ]
                    },
                    "post_build": {
                        "commands": [
                            "echo Pushing the Docker image...",
                            "docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$IMAGE_REPO_NAME:latest",
                            "echo Build completed on `date`",
                        ]
                    },
                },
            }),
            environment_variables={
                "AWS_DEFAULT_REGION": codebuild.BuildEnvironmentVariable(value=self.region),
                "AWS_ACCOUNT_ID": codebuild.BuildEnvironmentVariable(value=self.account),
                "IMAGE_REPO_NAME": codebuild.BuildEnvironmentVariable(value=self.ECR_REPO_NAME),
            },
        )

        NagSuppressions.add_resource_suppressions_by_path(self,
            f"/{self.stack_name}/CodeBuildRole/DefaultPolicy/Resource",
            [{"id": "AwsSolutions-IAM5", "reason": "CDK grants S3 read permissions for CodeBuild source bucket access"}]
        )

        # Lambda to trigger CodeBuild (reuses the same Lambda from AgentCore)
        build_trigger_role = iam.Role(self, "BuildTriggerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        build_trigger_role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/lambda/*"],
        ))
        build_trigger_role.add_to_policy(iam.PolicyStatement(
            actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
            resources=[build_project.project_arn],
        ))

        build_trigger_function = _lambda.Function(self, "BuildTriggerFunction",
            runtime=_lambda.Runtime.PYTHON_3_14,
            handler="index.handler",
            timeout=Duration.minutes(15),
            code=_lambda.Code.from_asset("lambda/func_build_trigger"),
            role=build_trigger_role,
        )

        NagSuppressions.add_resource_suppressions(build_trigger_role,
            [{"id": "AwsSolutions-IAM5", "reason": "Lambda requires CloudWatch logs wildcard"}],
            apply_to_children=True
        )

        # Custom Resource — triggers build when source hash changes
        source_hash = hashlib.md5(source_asset.asset_hash.encode(), usedforsecurity=False).hexdigest()[:8]
        trigger_build = CustomResource(self, "TriggerImageBuild",
            service_token=build_trigger_function.function_arn,
            properties={"ProjectName": build_project.project_name, "SourceHash": source_hash},
        )

        # ===== SageMaker Endpoint =====

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
        model.node.add_dependency(trigger_build)  # Ensure image exists before creating model

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
