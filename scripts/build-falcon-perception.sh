#!/usr/bin/env bash
set -euo pipefail

# Build and push the Falcon-Perception inference container image via CodeBuild.
# Usage: AWS_REGION=eu-west-2 ./scripts/build-falcon-perception.sh

REGION="${AWS_REGION:-${CDK_DEFAULT_REGION:-us-east-1}}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO_NAME="falcon-perception-inference"
PROJECT_NAME="falcon-perception-build-${REGION}"
BUCKET="falcon-perception-build-source-${ACCOUNT_ID}-${REGION}"
SOURCE_DIR="infrastructure/sagemaker/falcon-perception"
ROLE_NAME="falcon-perception-codebuild-role"

echo "Building Falcon-Perception in region: $REGION"

# Create ECR repository if it doesn't exist
aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$REGION" &>/dev/null || \
  aws ecr create-repository --repository-name "$REPO_NAME" --region "$REGION"

# Create S3 bucket if it doesn't exist
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
fi

# Ensure CodeBuild service role exists with required policies
if ! aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then
  echo "Creating CodeBuild service role..."
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "codebuild.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }'
fi

# Attach required policies (idempotent)
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsFullAccess
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess

ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query "Role.Arn" --output text)

# Create CodeBuild project if it doesn't exist
if ! aws codebuild batch-get-projects --names "$PROJECT_NAME" --region "$REGION" \
    --query "projects[0].name" --output text 2>/dev/null | grep -q "$PROJECT_NAME"; then
  echo "Creating CodeBuild project: $PROJECT_NAME"
  # Wait for IAM role propagation
  sleep 10
  aws codebuild create-project \
    --name "$PROJECT_NAME" \
    --source "type=S3,location=${BUCKET}/source.zip" \
    --artifacts type=NO_ARTIFACTS \
    --environment "type=LINUX_CONTAINER,image=aws/codebuild/standard:7.0,computeType=BUILD_GENERAL1_LARGE,privilegedMode=true,environmentVariables=[{name=AWS_DEFAULT_REGION,value=${REGION},type=PLAINTEXT},{name=AWS_ACCOUNT_ID,value=${ACCOUNT_ID},type=PLAINTEXT}]" \
    --service-role "$ROLE_ARN" \
    --region "$REGION"
fi

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
