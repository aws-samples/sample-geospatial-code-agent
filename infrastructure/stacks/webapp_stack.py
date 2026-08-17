from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_cloudfront_origins as origins,
    aws_cloudfront as cloudfront,
    CfnOutput,
    aws_s3 as s3,
)
from constructs import Construct
from cdk_nag import NagSuppressions

class WebAppStack(Stack):
    def __init__(self,
                 scope: Construct,
                 construct_id: str,
                 agent_stack,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        
        prefix = "GeospatialAgentWebApp"

        # Create Cognito user pool
        user_pool = cognito.UserPool(self,
            f"{prefix}UserPool",
            feature_plan=cognito.FeaturePlan.LITE,
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=True
            )
        )

        NagSuppressions.add_resource_suppressions(
            user_pool,
            [
                {"id": "AwsSolutions-COG3", "reason": "Advanced Security requires Cognito Plus feature plan. We do not use Advanced Security for this sample code."},
                {"id": "AwsSolutions-COG8", "reason": "The Plus feature plan is not required for this sample code. We intentionally use the Lite feature plan to keep costs low."},
            ]
        )

        user_pool_client_static_ui = cognito.UserPoolClient(self,
            f"{prefix}UserPoolClientStaticUI",
            user_pool=user_pool,
            generate_secret=False,  # Changed from True
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
        )

        # Create Cognito Identity Pool for AWS credentials
        identity_pool = cognito.CfnIdentityPool(self,
            f"{prefix}IdentityPool",
            allow_unauthenticated_identities=False,
            cognito_identity_providers=[
                cognito.CfnIdentityPool.CognitoIdentityProviderProperty(
                    client_id=user_pool_client_static_ui.user_pool_client_id,
                    provider_name=user_pool.user_pool_provider_name,
                )
            ],
        )

        # IAM role for authenticated users
        authenticated_role = iam.Role(self,
            f"{prefix}AuthenticatedRole",
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {"cognito-identity.amazonaws.com:aud": identity_pool.ref},
                    "ForAnyValue:StringLike": {"cognito-identity.amazonaws.com:amr": "authenticated"},
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )

        # Grant AgentCore invoke permission to authenticated users
        authenticated_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[
                agent_stack.agent_runtime.attr_agent_runtime_arn,
                f"{agent_stack.agent_runtime.attr_agent_runtime_arn}/*",
                ],
        ))

        NagSuppressions.add_resource_suppressions(
            authenticated_role,
            [{"id": "AwsSolutions-IAM5", "reason": "AgentCore invocation scoped to specific agent runtime ARN"}],
            apply_to_children=True
        )

        # Attach role to identity pool
        cognito.CfnIdentityPoolRoleAttachment(self,
            f"{prefix}IdentityPoolRoleAttachment",
            identity_pool_id=identity_pool.ref,
            roles={"authenticated": authenticated_role.role_arn},
        )

        # S3 bucket for CloudFront access logs
        cloudfront_logs_bucket = s3.Bucket(
            self, f"{prefix}CloudFrontLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER
        )
        NagSuppressions.add_resource_suppressions(
            cloudfront_logs_bucket,
            [{"id": "AwsSolutions-S1", "reason": "This is the CloudFront access logs bucket itself"}]
        )

        # S3 bucket for React UI access logs
        ui_logs_bucket = s3.Bucket(
            self, f"{prefix}ReactUILogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )
        NagSuppressions.add_resource_suppressions(
            ui_logs_bucket,
            [{"id": "AwsSolutions-S1", "reason": "This is the access logs bucket itself"}]
        )

        # S3 bucket for React UI static hosting
        ui_bucket = s3.Bucket(
            self, f"{prefix}ReactUIBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=ui_logs_bucket,
            server_access_logs_prefix="s3-access-logs/",
        )

        # CloudFront distribution for React UI with OAC
        react_distribution = cloudfront.Distribution(
            self, f"{prefix}ReactCfDist",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(ui_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            log_bucket=cloudfront_logs_bucket,
            log_file_prefix="react-cloudfront-logs/",
        )

        NagSuppressions.add_resource_suppressions(
            react_distribution,
            [{"id": "AwsSolutions-CFR4", "reason": "Using default CloudFront domain without custom SSL certificate. TLS 1.2 is set via minimum_protocol_version."}]
        )

        # Output CloudFront URL
        CfnOutput(self, "ReactUIURL", value=f"https://{react_distribution.domain_name}")
        CfnOutput(self, "CognitoUserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "CognitoClientIdStaticUI", value=user_pool_client_static_ui.user_pool_client_id)
        CfnOutput(self, "CognitoIdentityPoolId", value=identity_pool.ref)
        CfnOutput(self, "AgentRuntimeArn", value=agent_stack.agent_runtime.attr_agent_runtime_arn)
        CfnOutput(self, "ReactUIBucketName", value=ui_bucket.bucket_name)
        CfnOutput(self, "ReactDistributionId", value=react_distribution.distribution_id)