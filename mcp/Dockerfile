FROM python:3.12.12-slim-bookworm AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && useradd --uid 10001 --create-home app
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]

# Optional GPU image. Keep the default final stage lightweight for the regular stack.
FROM runtime-base AS kev
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-kev.lock .
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cu128 \
      --extra-index-url https://pypi.org/simple \
      -r requirements-kev.lock
RUN mkdir -p /model-cache && chown 10001:10001 /model-cache
RUN git init /opt/kev \
    && git -C /opt/kev remote add origin https://github.com/jaredpalmer/kev.git \
    && git -C /opt/kev fetch --depth 1 origin 5e42a7a03f28134853dd3ff77461457e921e5ec1 \
    && git -C /opt/kev checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/kev rev-parse HEAD)" = "5e42a7a03f28134853dd3ff77461457e921e5ec1" \
    && echo "a9922dbb89acdef78299fd2b4a8c3f7f0fa1b2bc08b55595b6926fa785a9c466  /opt/kev/uv.lock" | sha256sum -c - \
    && pip install --no-deps /opt/kev
COPY server.py smoke.py decision_contract.py decision_context.py decision_retrieval.py decision_runtime.py kev_adapter.py kev-artifact-checksums.json ./
COPY prepare_kev_artifacts.py ./
ENV KEV_SOURCE=/opt/kev KEV_DEVICE=cuda \
    KEV_CHECKPOINT=/model-cache/artifacts/kev \
    KEV_BASE=/model-cache/artifacts/base \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
USER 10001:10001

# Docker uses the last stage when no target is selected.
FROM runtime-base AS default
COPY server.py smoke.py ./
