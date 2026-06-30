# syntax=docker/dockerfile:1.7
#
# Roofing Estimator — FastAPI + HTMX web app on uvicorn:8000.
#
# Build:
#   docker build -t roofing-estimator:dev .
# Run:
#   docker run --rm -p 8000:8000 -v roofing_data:/home/data roofing-estimator:dev
# Then open http://localhost:8000
#
# Set ADMIN_PASSWORD to turn on the login gate (off by default for local use).

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/home/data

WORKDIR /app

# Install Python deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code + bundled agent-prompt fallback (synced from S3 in prod).
COPY app ./app
COPY _agent_prompts ./_agent_prompts

# Persistent data dir for projects, answers and uploaded documents. Mount a
# volume here in production so it survives container restarts.
RUN mkdir -p /home/data

EXPOSE 8000

# Single worker — sessions and in-memory state assume one process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
