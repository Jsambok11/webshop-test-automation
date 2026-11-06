FROM python:3.12-slim-bookworm

WORKDIR /app
# Build context is services/ so the shared module can be included.
COPY frontend-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/servicekit.py frontend-service/app.py ./
COPY frontend-service/templates/ templates/

# Do not run as root inside the container.
RUN useradd --create-home --uid 10001 appuser
USER 10001

EXPOSE 5000
# gunicorn instead of Flask's development server. SECRET_KEY comes from a
# Kubernetes Secret, so several workers and pods share sessions.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "30",      "--access-logfile", "-", "--access-logformat", "%(h)s \"%(r)s\" %(s)s %(b)s %(M)sms rid=%({x-request-id}o)s",      "app:app"]
