FROM python:3.12.10-slim
WORKDIR /app
COPY services/ontology_ui/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY services/ontology_ui /app/services/ontology_ui
USER 65532:65532
CMD ["uvicorn", "services.ontology_ui.app:production_app", "--factory", "--host", "0.0.0.0", "--port", "8010"]
