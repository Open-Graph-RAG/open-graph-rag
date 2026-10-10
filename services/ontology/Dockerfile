FROM python:3.12.10-slim
WORKDIR /app
COPY services/ontology/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY services /app/services
COPY ontology /app/ontology
COPY postgres/migrations /app/postgres/migrations
USER 65532:65532
CMD ["uvicorn", "services.ontology.api:production_app", "--factory", "--host", "0.0.0.0", "--port", "8010"]
