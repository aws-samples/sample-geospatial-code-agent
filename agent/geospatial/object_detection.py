"""Object detection using Falcon-Perception via SageMaker endpoint or EC2."""
import base64
import io
import json
import os

import boto3
import numpy as np

# --- Configuration ---
# Set to "sagemaker" to use the SageMaker endpoint, or "ec2" to use the EC2 instance.
OBJECT_DETECTION_BACKEND = os.environ.get("OBJECT_DETECTION_BACKEND", "sagemaker")

ENDPOINT_NAME = os.environ.get("OBJECT_DETECTION_ENDPOINT_NAME", "falcon-perception-object-detection")
EC2_ENDPOINT_URL = os.environ.get("OBJECT_DETECTION_EC2_URL", "http://localhost:18080")


def _invoke_sagemaker(payload: str) -> list[dict]:
    sagemaker_runtime = boto3.client("sagemaker-runtime")
    response = sagemaker_runtime.invoke_endpoint(
        EndpointName=ENDPOINT_NAME,
        ContentType="application/json",
        Body=payload,
    )
    return json.loads(response["Body"].read())


def _invoke_ec2(payload: str) -> list[dict]:
    import urllib.request
    req = urllib.request.Request(
        f"{EC2_ENDPOINT_URL}/invocations",
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def detect_objects(
    image: np.ndarray,
    query: str,
) -> list[dict]:
    """Detect objects in a high-resolution image using open-vocabulary detection.

    Uses Falcon-Perception via a SageMaker endpoint for natural language-driven
    object detection and instance segmentation. Works best with high-resolution
    images from get_high_resolution_image().

    Args:
        image: H x W x 3 uint8 RGB image (e.g., from get_high_resolution_image()['rgb']).
        query: Natural language description of what to detect (e.g., "airplane",
            "car", "ship", "building"). Open vocabulary - not limited to a fixed
            set of classes.

    Returns:
        List of detection dicts, each containing:
            - 'bbox' (tuple): (x_min, y_min, x_max, y_max) in pixel coordinates.
            - 'center' (tuple): (x, y) center in pixel coordinates.
            - 'size' (tuple): (width, height) in pixels.
            - 'mask' (np.ndarray): H x W boolean mask for this instance.

    Example:
        >>> result = get_high_resolution_image(polygon_coordinates=AOI_COORDINATES)
        >>> detections = detect_objects(result['rgb'], "airplane")
        >>> print(f"Found {len(detections)} airplanes")
        >>> for d in detections:
        ...     print(f"  at {d['center']}, size {d['size']}")
    """
    from PIL import Image
    from pycocotools import mask as mask_utils

    h, w = image.shape[:2]

    # Encode image as JPEG for efficient transfer
    pil_image = Image.fromarray(image)
    buffer = io.BytesIO()
    pil_image.save(buffer, format="JPEG", quality=90)
    image_b64 = base64.b64encode(buffer.getvalue()).decode()

    # Invoke endpoint
    payload = json.dumps({"image": image_b64, "query": query})
    if OBJECT_DETECTION_BACKEND == "ec2":
        preds = _invoke_ec2(payload)
    else:
        preds = _invoke_sagemaker(payload)

    # Convert normalized coords to pixel coords and decode masks
    detections = []
    for p in preds:
        cx = p["xy"]["x"] * w
        cy = p["xy"]["y"] * h
        bw = p["hw"]["w"] * w
        bh = p["hw"]["h"] * h

        x_min = int(cx - bw / 2)
        y_min = int(cy - bh / 2)
        x_max = int(cx + bw / 2)
        y_max = int(cy + bh / 2)

        # Decode RLE mask
        rle = p["mask_rle"]
        rle_coco = {"size": rle["size"], "counts": rle["counts"].encode("utf-8")}
        mask = mask_utils.decode(rle_coco).astype(bool)

        detections.append({
            "bbox": (x_min, y_min, x_max, y_max),
            "center": (int(cx), int(cy)),
            "size": (int(bw), int(bh)),
            "mask": mask,
        })

    print(f"Detected {len(detections)} '{query}' objects")
    return detections
