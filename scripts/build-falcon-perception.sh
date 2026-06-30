#!/usr/bin/env bash
set -euo pipefail

# Build and push the Falcon-Perception inference container image via CodeBuild.
# Usage: ./scripts/build-falcon-perception.sh

REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO_NAME="falcon-perception-inference"
PROJECT_NAME="falcon-perception-build"
BUCKET="falcon-perception-build-source-${ACCOUNT_ID}"
SOURCE_DIR="infrastructure/sagemaker/falcon-perception"

# Create ECR repository if it doesn't exist
aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$REGION" &>/dev/null || \
  aws ecr create-repository --repository-name "$REPO_NAME" --region "$REGION"

# Create S3 bucket if it doesn't exist
aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || \
  aws s3 mb "s3://$BUCKET" --region "$REGION"

# Create CodeBuild project if it doesn't exist
aws codebuild batch-get-projects --names "$PROJECT_NAME" --region "$REGION" --query "projects[0].name" --output text 2>/dev/null | grep -q "$PROJECT_NAME" || \
  aws codebuild create-project \
    --name "$PROJECT_NAME" \
    --source "type=S3,location=${BUCKET}/source.zip" \
    --artifacts type=NO_ARTIFACTS \
    --environment "type=LINUX_CONTAINER,image=aws/codebuild/standard:7.0,computeType=BUILD_GENERAL1_LARGE,privilegedMode=true" \
    --service-role "falcon-perception-codebuild-role" \
    --region "$REGION"

# Upload source
echo "Packaging source..."
TMPFILE="/tmp/falcon-perception-source-$$.zip"
rm -f "$TMPFILE"
(cd "$SOURCE_DIR" && zip "$TMPFILE" buildspec.yml Dockerfile handler.py)
aws s3 cp "$TMPFILE" "s3://${BUCKET}/source.zip" --region "$REGION"
rm -f "$TMPFILE"

# Trigger build
echo "Starting CodeBuild..."
BUILD_ID=$(aws codebuild start-build \
  --project-name "$PROJECT_NAME" \
  --region "$REGION" \
  --query "build.id" --output text)

echo "Build started: $BUILD_ID"
echo "Monitor with: aws codebuild batch-get-builds --ids $BUILD_ID --region $REGION --query \"builds[0].buildStatus\""
