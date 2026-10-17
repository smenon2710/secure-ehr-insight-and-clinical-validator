# One container runs both processes: the FastAPI backend on 127.0.0.1:8000 (internal only) and the
# Streamlit UI on 0.0.0.0:8501. Configuration comes from --env-file .env at `docker run` time.
#
#   docker build -t fde-project-2:latest .
#   docker run -d --name fde-project-2 --restart unless-stopped --env-file .env -p 8501:8501 fde-project-2:latest
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

# curl: for `docker exec fde-project-2 curl -s localhost:8000/...` diagnostics (docs/TROUBLESHOOTING.md)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app

# Dependencies first so code changes don't reinstall them. requirements.txt is exported from uv.lock
# with CPU-only torch (see its header).
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN mkdir -p "$HF_HOME" && chown -R app:app /app
USER app

# Bake the embedding model into the image so startup doesn't depend on Hugging Face being reachable
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('NeuML/bioclinical-modernbert-base-embeddings')"

COPY --chown=app:app .streamlit/ .streamlit/
COPY --chown=app:app src/ src/
COPY --chown=app:app scripts/ scripts/
COPY --chown=app:app evals/ evals/

EXPOSE 8501

# If either process exits, the container exits, so `--restart unless-stopped` brings both back.
CMD ["bash", "-c", "\
: \"${JWT_SECRET:?JWT_SECRET is not set. Add it to .env (see docs/CHANGELOG.md).}\"; \
uvicorn src.api.main:app --host 127.0.0.1 --port 8000 & \
streamlit run src/ui/app.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true & \
wait -n; exit $?"]
