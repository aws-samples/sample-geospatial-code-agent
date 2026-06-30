"""
SageMaker inference handler for Falcon-Perception.
Serves HTTP on port 8080 with /ping and /invocations endpoints.
"""
import io
import json
import base64
import logging
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler

import torch
from PIL import Image
from falcon_perception import (
    PERCEPTION_MODEL_ID,
    build_prompt_for_task,
    load_and_prepare_model,
    setup_torch_config,
)
from falcon_perception.paged_inference import PagedInferenceEngine, SamplingParams, Sequence
from falcon_perception.data import ImageProcessor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

engine = None
tokenizer = None
image_processor = None


def load_model():
    global engine, tokenizer, image_processor
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    log.info(f"Loading model on {device} (CUDA available: {torch.cuda.is_available()})")
    setup_torch_config()
    model, tokenizer, model_args = load_and_prepare_model(
        hf_model_id=PERCEPTION_MODEL_ID,
        device=device,
        dtype="float32",
        compile=True,
    )
    image_processor = ImageProcessor(patch_size=16, merge_size=1)
    # CUDA graph capture only makes sense on GPU
    use_cuda = device.startswith("cuda")
    engine = PagedInferenceEngine(
        model, tokenizer, image_processor,
        max_batch_size=2,
        max_seq_length=8192,
        n_pages=128,
        page_size=128,
        prefill_length_limit=8192,
        enable_hr_cache=False,
        capture_cudagraph=use_cuda,
        kernel_options={"BLOCK_M": 64, "BLOCK_N": 64, "num_stages": 1},
    )
    log.info("Model loaded and engine ready.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # suppress default per-request logs
        pass

    def _send_json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/ping":
            status = 200 if engine is not None else 503
            body = b"OK" if engine is not None else b"Model not loaded"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/invocations":
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            image_bytes = base64.b64decode(body["image"])
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            query = body["query"]
            task = body.get("task", "segmentation")

            prompt = build_prompt_for_task(query, task)
            stop_token_ids = [tokenizer.eos_token_id, tokenizer.end_of_query_token_id]
            sampling_params = SamplingParams(stop_token_ids=stop_token_ids)

            sequences = [Sequence(
                text=prompt,
                image=image,
                min_image_size=256,
                max_image_size=1024,
                task=task,
            )]

            with torch.inference_mode():
                engine.generate(sequences, sampling_params=sampling_params, use_tqdm=False, print_stats=False)

            seq = sequences[0]
            aux = seq.output_aux

            predictions = []
            # bboxes_raw is interleaved [{"x":.., "y":..}, {"h":.., "w":..}, ...]
            bboxes = aux.bboxes_raw
            paired = [(bboxes[i], bboxes[i+1]) for i in range(0, len(bboxes)-1, 2)]
            if task == "segmentation":
                for i, mask_rle in enumerate(aux.masks_rle):
                    pred = {"mask_rle": mask_rle}
                    if i < len(paired):
                        pred["xy"] = paired[i][0]   # {"x": float, "y": float}
                        pred["hw"] = paired[i][1]   # {"h": float, "w": float}
                    predictions.append(pred)
            elif task == "detection":
                for xy, hw in paired:
                    predictions.append({"xy": xy, "hw": hw})

            self._send_json(200, predictions)

        except (KeyError, ValueError) as e:
            log.warning(f"Bad request: {e}")
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            log.error(traceback.format_exc())
            self._send_json(500, {"error": str(e)})


if __name__ == "__main__":
    load_model()
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    log.info("Server listening on port 8080")
    server.serve_forever()
