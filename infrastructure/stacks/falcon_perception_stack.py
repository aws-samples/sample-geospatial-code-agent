import hashlib

from aws_cdk import (
    Stack, CfnOutput, Duration, CustomResource, RemovalPolicy,
    aws_ecr as ecr, aws_iam as iam, aws_sagemaker as sagemaker,
    aws_ec2 as ec2, aws_codebuild as codebuild, aws_s3_assets as s3_assets,
    aws_lambda as _lambda, aws_kms as kms,
    aws_applicationautoscaling as appscaling,
    aws_cloudwatch as cloudwatch,
)
from constructs import Construct
from cdk_nag import NagSuppressions


class FalconPerceptionStack(Stack):
    """SageMaker endpoint for Falcon-Perception object detection.

    Includes a CodeBuild pipeline that builds the container image automatically
    when the source changes (Dockerfile, handler.py, buildspec.yml).

    Uses Inference Components with ManagedInstanceScaling (min=0) and
    Application Auto Scaling for fully automatic scale-to-zero:
        - Scales IN to 0 after 60 minutes of no invocations
        - Scales OUT from 0 automatically when an invocation is attempted
          (triggered by NoCapacityInvocationFailures CloudWatch metric)
        - Cold start takes ~5-8 minutes (instance provisioning + model load)
    """

    ENDPOINT_NAME = "falcon-perception-object-detection"
    INFERENCE_COMPONENT_NAME = "falcon-perception-model"
    ECR_REPO_NAME = "falcon-perception-inference"

    def __init__(self, scope: Construct, id: str, vpc: ec2.IVpc, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ===== Container Image Build Pipeline =====

        # ECR repository — created automatically on first deploy, retained on stack deletion
        # to preserve built images across redeployments.
        repo = ecr.Repository(self, "Repo",
            repository_name=self.ECR_REPO_NAME,
            removal_policy=RemovalPolicy.RETAIN,
            empty_on_delete=False,
        )

        source_asset = s3_assets.Asset(self, "SourceAsset",
            path="sagemaker/falcon-perception"
        )

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

        # Lambda to trigger CodeBuild
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

        sm_role = iam.Role(self, "SageMakerRole", assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"))
        sm_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"))
        repo.grant_pull(sm_role)

        endpoint_sg = ec2.SecurityGroup(self, "EndpointSG", vpc=vpc, description="Falcon-Perception endpoint SG")

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
        model.node.add_dependency(trigger_build)

        # Endpoint config — ManagedInstanceScaling min=0 enables scale-to-zero.
        # ml.g6e.xlarge: L40S GPU, 48GB VRAM — handles large input images without OOM.
        endpoint_config = sagemaker.CfnEndpointConfig(self, "EndpointConfig",
            execution_role_arn=sm_role.role_arn,
            vpc_config=sagemaker.CfnEndpointConfig.VpcConfigProperty(
                security_group_ids=[endpoint_sg.security_group_id],
                subnets=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnet_ids,
            ),
            production_variants=[sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="primary",
                instance_type="ml.g6e.xlarge",
                initial_instance_count=1,
                managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                    min_instance_count=0,
                    max_instance_count=1,
                    status="ENABLED",
                ),
                routing_config=sagemaker.CfnEndpointConfig.RoutingConfigProperty(
                    routing_strategy="LEAST_OUTSTANDING_REQUESTS",
                ),
                container_startup_health_check_timeout_in_seconds=600,
                model_data_download_timeout_in_seconds=600,
            )]
        )
        endpoint_config.node.add_dependency(sm_role)

        self.endpoint_name = self.ENDPOINT_NAME
        endpoint = sagemaker.CfnEndpoint(self, "Endpoint",
            endpoint_name=self.endpoint_name,
            endpoint_config_name=endpoint_config.attr_endpoint_config_name,
        )
        endpoint.add_dependency(endpoint_config)

        # Inference Component — required for scale-to-zero support.
        inference_component = sagemaker.CfnInferenceComponent(self, "InferenceComponent",
            endpoint_name=self.endpoint_name,
            inference_component_name=self.INFERENCE_COMPONENT_NAME,
            variant_name="primary",
            specification=sagemaker.CfnInferenceComponent.InferenceComponentSpecificationProperty(
                model_name=model.model_name,
                compute_resource_requirements=sagemaker.CfnInferenceComponent.InferenceComponentComputeResourceRequirementsProperty(
                    number_of_accelerator_devices_required=1,
                    min_memory_required_in_mb=512,
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

        # ===== Application Auto Scaling =====
        # Enables fully automatic scale-to-zero and scale-from-zero.
        # No manual start/stop scripts needed.
        #
        # Strategy: Two step scaling policies (no target tracking).
        # - Scale OUT: Triggered by NoCapacityInvocationFailures (instant wake-up)
        # - Scale IN:  Triggered after 60 consecutive minutes of zero invocations

        # Register inference component as a scalable target (min=0, max=1)
        scalable_target = appscaling.CfnScalableTarget(self, "ScalableTarget",
            service_namespace="sagemaker",
            resource_id=f"inference-component/{self.INFERENCE_COMPONENT_NAME}",
            scalable_dimension="sagemaker:inference-component:DesiredCopyCount",
            min_capacity=0,
            max_capacity=1,
            role_arn=sm_role.role_arn,
        )
        scalable_target.node.add_dependency(inference_component)

        # Step scaling policy for SCALE-IN — reduces copies to 0.
        # Only triggered after 60 consecutive minutes of zero invocations.
        scale_in_policy = appscaling.CfnScalingPolicy(self, "ScaleInStepPolicy",
            policy_name="falcon-perception-scale-in-after-idle",
            policy_type="StepScaling",
            service_namespace="sagemaker",
            resource_id=f"inference-component/{self.INFERENCE_COMPONENT_NAME}",
            scalable_dimension="sagemaker:inference-component:DesiredCopyCount",
            step_scaling_policy_configuration=appscaling.CfnScalingPolicy.StepScalingPolicyConfigurationProperty(
                adjustment_type="ExactCapacity",
                metric_aggregation_type="Average",
                cooldown=3600,
                step_adjustments=[
                    appscaling.CfnScalingPolicy.StepAdjustmentProperty(
                        metric_interval_upper_bound=0,
                        scaling_adjustment=0,  # Set to exactly 0 copies
                    )
                ],
            ),
        )
        scale_in_policy.node.add_dependency(scalable_target)

        # CloudWatch alarm for scale-in — requires 60 consecutive 1-minute periods
        # with zero invocations before triggering. This ensures the endpoint
        # stays up for at least 60 minutes after last use.
        cloudwatch.CfnAlarm(self, "IdleScaleInAlarm",
            alarm_name="falcon-perception-idle-60min",
            alarm_description="Triggers scale-in after 60 minutes of zero invocations",
            alarm_actions=[scale_in_policy.ref],
            namespace="AWS/SageMaker",
            metric_name="Invocations",
            dimensions=[cloudwatch.CfnAlarm.DimensionProperty(
                name="InferenceComponentName",
                value=self.INFERENCE_COMPONENT_NAME,
            )],
            statistic="Sum",
            period=60,              # 1-minute periods
            evaluation_periods=60,  # 60 periods = 60 minutes
            datapoints_to_alarm=60, # All 60 must be below threshold
            comparison_operator="LessThanOrEqualToThreshold",
            threshold=0,
            treat_missing_data="notBreaching",  # No data (e.g., during cold start) = not idle
        )

        # Step scaling policy for SCALE-OUT from zero — provisions an instance
        # when an invocation fails due to no capacity.
        scale_out_policy = appscaling.CfnScalingPolicy(self, "ScaleOutPolicy",
            policy_name="falcon-perception-scale-out-from-zero",
            policy_type="StepScaling",
            service_namespace="sagemaker",
            resource_id=f"inference-component/{self.INFERENCE_COMPONENT_NAME}",
            scalable_dimension="sagemaker:inference-component:DesiredCopyCount",
            step_scaling_policy_configuration=appscaling.CfnScalingPolicy.StepScalingPolicyConfigurationProperty(
                adjustment_type="ChangeInCapacity",
                metric_aggregation_type="Maximum",
                cooldown=60,
                step_adjustments=[
                    appscaling.CfnScalingPolicy.StepAdjustmentProperty(
                        metric_interval_lower_bound=0,
                        scaling_adjustment=1,
                    )
                ],
            ),
        )
        scale_out_policy.node.add_dependency(scalable_target)

        # CloudWatch alarm for scale-out — fires when the endpoint receives an
        # invocation but has no instances to serve it.
        cloudwatch.CfnAlarm(self, "NoCapacityAlarm",
            alarm_name="falcon-perception-no-capacity",
            alarm_description="Triggers scale-out when endpoint is invoked with 0 instances",
            alarm_actions=[scale_out_policy.ref],
            namespace="AWS/SageMaker",
            metric_name="NoCapacityInvocationFailures",
            dimensions=[cloudwatch.CfnAlarm.DimensionProperty(
                name="InferenceComponentName",
                value=self.INFERENCE_COMPONENT_NAME,
            )],
            statistic="Sum",
            period=60,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator="GreaterThanThreshold",
            threshold=0,
            treat_missing_data="notBreaching",
        )

        # ===== Outputs =====
        CfnOutput(self, "EndpointName", value=self.endpoint_name)
        CfnOutput(self, "InferenceComponentName", value=self.INFERENCE_COMPONENT_NAME)

        # ===== Nag Suppressions =====
        NagSuppressions.add_resource_suppressions(sm_role,
            [
                {"id": "AwsSolutions-IAM4", "reason": "SageMaker managed policy required for endpoint operation"},
                {"id": "AwsSolutions-IAM5", "reason": "ECR access requires wildcards"}
            ], apply_to_children=True)
