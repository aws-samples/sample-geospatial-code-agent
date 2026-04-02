#!/bin/bash
set -e

DEPLOY_CDK=false
if [[ "$1" == "--cdk" ]]; then
    DEPLOY_CDK=true
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$SCRIPT_DIR/../infrastructure"
UI_DIR="$SCRIPT_DIR/../user-interface"

if [[ "$DEPLOY_CDK" == true ]]; then
    cd "$INFRA_DIR"
    echo "=== Deploying CDK stacks ==="
    cdk deploy --all --require-approval never
else
    echo "=== Skipping CDK deployment (use --cdk to deploy) ==="
fi

cd "$SCRIPT_DIR/.."

# Get outputs from CloudFormation
STACK_NAME="GeospatialWebAppStack"
get_output() {
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET_NAME=$(get_output "ReactUIBucketName")
DISTRIBUTION_ID=$(get_output "ReactDistributionId")
COGNITO_USER_POOL_ID=$(get_output "CognitoUserPoolId")
COGNITO_CLIENT_ID_STATIC_UI=$(get_output "CognitoClientIdStaticUI")
COGNITO_IDENTITY_POOL_ID=$(get_output "CognitoIdentityPoolId")
AGENT_RUNTIME_ARN=$(get_output "AgentRuntimeArn")
AWS_REGION=$(echo "$AGENT_RUNTIME_ARN" | cut -d: -f4)

echo "=== Generating .env file ==="
cat > "$UI_DIR/.env" << EOF
VITE_COGNITO_USER_POOL_ID=$COGNITO_USER_POOL_ID
VITE_COGNITO_CLIENT_ID_STATIC_UI=$COGNITO_CLIENT_ID_STATIC_UI
VITE_COGNITO_IDENTITY_POOL_ID=$COGNITO_IDENTITY_POOL_ID
VITE_AGENT_RUNTIME_ARN=$AGENT_RUNTIME_ARN
VITE_AWS_REGION=$AWS_REGION
EOF

echo "=== Building React UI ==="
cd "$UI_DIR"
npm install
npm run build

echo "=== Deploying UI to S3 ==="
aws s3 sync dist/ "s3://$BUCKET_NAME" --delete

echo "=== Invalidating CloudFront cache ==="
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/*"

echo "=== Deployment complete ==="
REACT_URL=$(get_output "ReactUIURL")
echo "React UI URL: $REACT_URL"
echo "User Pool ID: $COGNITO_USER_POOL_ID"
