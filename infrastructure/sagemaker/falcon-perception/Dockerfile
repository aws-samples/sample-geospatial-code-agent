FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-venv python3-pip git gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*

# Install torch+cu128 from PyTorch index (must be explicit - not on PyPI)
RUN pip install --no-cache-dir --break-system-packages \
    torch torchvision --index-url https://download.pytorch.org/whl/cu128

# Clone repo and install falcon-perception (torch already satisfied from above)
RUN git clone https://github.com/tiiuae/Falcon-Perception.git /opt/Falcon-Perception && \
    pip install --no-cache-dir --break-system-packages /opt/Falcon-Perception

# Ensure pip-installed cuDNN 9.19 libs are found BEFORE any system libs
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/dist-packages/nvidia/cuda_runtime/lib:${LD_LIBRARY_PATH}

# Validate Python.h is available (required by Triton JIT at runtime)
RUN test -f /usr/include/python3.12/Python.h || { echo "FATAL: Python.h not found — python3-dev not installed correctly"; exit 1; }
RUN python3 --version && python3 -c "import sys; assert sys.version_info >= (3, 11), f'Need Python 3.11+, got {sys.version}'"

# Validate
RUN python3 -c "import falcon_perception; print('falcon-perception installed')"

COPY handler.py /opt/handler.py

EXPOSE 8080

ENTRYPOINT ["python3", "/opt/handler.py"]
