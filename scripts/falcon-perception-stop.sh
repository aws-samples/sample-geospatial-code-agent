#!/usr/bin/env bash
# Stop the Falcon-Perception object detection endpoint by scaling copies to 0.
# The endpoint infrastructure remains deployed — only the model copy is unloaded.
# This stops GPU instance billing within minutes.
#
# Usage: ./scripts/falcon-perception-stop.sh [--region REGION]
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
INFERENCE_COMPONENT_NAME="falcon-perception-model"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "Scaling down inference component '${INFERENCE_COMPONENT_NAME}' in ${REGION}..."

aws sagemaker update-inference-component-runtime-config \
    --inference-component-name "${INFERENCE_COMPONENT_NAME}" \
    --desired-runtime-config "CopyCount=0" \
    --region "${REGION}"

echo "✓ Stop requested. The instance will scale down shortly (1-2 minutes)."
echo "  No GPU billing once the instance is released."
echo "  Run ./scripts/falcon-perception-start.sh to restart (~5-8 min cold start)."
