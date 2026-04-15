#!/bin/bash
set -e

USE_LOCAL_AGENT=false
if [ "$1" = "--local" ]; then
    USE_LOCAL_AGENT=true
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_DIR="$SCRIPT_DIR/../user-interface"

STACK_NAME="GeospatialWebAppStack"

echo "=== Fetching configuration from CloudFormation ==="

get_output() {
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || echo ""
}

COGNITO_USER_POOL_ID=$(get_output "CognitoUserPoolId")
COGNITO_CLIENT_ID_STATIC_UI=$(get_output "CognitoClientIdStaticUI")
COGNITO_IDENTITY_POOL_ID=$(get_output "CognitoIdentityPoolId")
AGENT_RUNTIME_ARN=$(get_output "AgentRuntimeArn")

if [ "$USE_LOCAL_AGENT" = "false" ] && [ -z "$AGENT_RUNTIME_ARN" ]; then
    echo "Error: Could not retrieve stack outputs. Make sure the stack '$STACK_NAME' is deployed."
    exit 1
fi

AWS_REGION=$(echo "$AGENT_RUNTIME_ARN" | cut -d: -f4)
if [ -z "$AWS_REGION" ]; then
    AWS_REGION="us-east-1"
fi

echo "=== Generating .env.local file ==="
if [ "$USE_LOCAL_AGENT" = "true" ]; then
    # Don't set AGENT_RUNTIME_ARN — the UI will use /invocations (proxied by Vite)
    cat > "$UI_DIR/.env.local" << EOF
VITE_COGNITO_USER_POOL_ID=$COGNITO_USER_POOL_ID
VITE_COGNITO_CLIENT_ID_STATIC_UI=$COGNITO_CLIENT_ID_STATIC_UI
VITE_COGNITO_IDENTITY_POOL_ID=$COGNITO_IDENTITY_POOL_ID
VITE_AGENT_RUNTIME_ARN=
VITE_AWS_REGION=$AWS_REGION
EOF
else
    cat > "$UI_DIR/.env.local" << EOF
VITE_COGNITO_USER_POOL_ID=$COGNITO_USER_POOL_ID
VITE_COGNITO_CLIENT_ID_STATIC_UI=$COGNITO_CLIENT_ID_STATIC_UI
VITE_COGNITO_IDENTITY_POOL_ID=$COGNITO_IDENTITY_POOL_ID
VITE_AGENT_RUNTIME_ARN=$AGENT_RUNTIME_ARN
VITE_AWS_REGION=$AWS_REGION
EOF
fi

echo "Configuration retrieved:"
echo "  Region: $AWS_REGION"
echo "  Local agent: $USE_LOCAL_AGENT"
echo ""

cd "$UI_DIR"

if [ ! -d "node_modules" ]; then
    echo "=== Installing dependencies ==="
    npm install
fi

echo "=== Starting development server ==="
npm start
