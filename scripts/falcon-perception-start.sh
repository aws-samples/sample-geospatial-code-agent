#!/usr/bin/env bash
# Start the Falcon-Perception object detection endpoint by scaling copies to 1.
# Provisions a GPU instance and loads the model (~5-8 minutes cold start).
#
# Usage: ./scripts/falcon-perception-start.sh [--region REGION] [--wait]
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
INFERENCE_COMPONENT_NAME="falcon-perception-model"
WAIT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --wait) WAIT=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "Scaling up inference component '${INFERENCE_COMPONENT_NAME}' in ${REGION}..."

# Wait for any in-progress operations to complete before updating
while true; do
    STATUS=$(aws sagemaker describe-inference-component \
        --inference-component-name "${INFERENCE_COMPONENT_NAME}" \
        --region "${REGION}" \
        --query "InferenceComponentStatus" \
        --output text 2>/dev/null || echo "NOT_FOUND")
    case "$STATUS" in
        InService|Failed|NOT_FOUND)
            break ;;
        *)
            echo "  Current status: ${STATUS} — waiting for stable state..."
            sleep 15 ;;
    esac
done

aws sagemaker update-inference-component-runtime-config \
    --inference-component-name "${INFERENCE_COMPONENT_NAME}" \
    --desired-runtime-config "CopyCount=1" \
    --region "${REGION}"

echo "✓ Start requested. Instance provisioning + model loading takes ~5-8 minutes."

if [ "$WAIT" = true ]; then
    echo "Waiting for inference component to become InService..."
    while true; do
        STATUS=$(aws sagemaker describe-inference-component \
            --inference-component-name "${INFERENCE_COMPONENT_NAME}" \
            --region "${REGION}" \
            --query "InferenceComponentStatus" \
            --output text)
        if [ "$STATUS" = "InService" ]; then
            echo "✓ Endpoint is InService and ready for requests."
            break
        elif [ "$STATUS" = "Failed" ]; then
            echo "✗ Inference component failed to start."
            aws sagemaker describe-inference-component \
                --inference-component-name "${INFERENCE_COMPONENT_NAME}" \
                --region "${REGION}" \
                --query "FailureReason" \
                --output text
            exit 1
        fi
        echo "  Status: ${STATUS} — waiting 30s..."
        sleep 30
    done
fi
