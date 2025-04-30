# Gunakan base image RunPod dengan CUDA dan PyTorch
FROM runpod/base:0.6.3-cuda11.8.0

WORKDIR /app

# Install dependensi sistem
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    tzdata \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Salin file requirements
COPY requirements.txt .

# Install dependensi Python
RUN pip install --no-cache-dir -r requirements.txt

# Salin kode handler
COPY handler.py .

# Perintah default
CMD ["python", "-u", "handler.py"]