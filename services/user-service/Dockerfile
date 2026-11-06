FROM python:3.12-slim-bookworm

# Microsoft ODBC driver for SQL Server, from the repo that matches this image's
# Debian version (12 / bookworm). One layer, cleaned up afterwards.
RUN apt-get update  && apt-get install -y --no-install-recommends curl gnupg ca-certificates  && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc     | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg  && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main"     > /etc/apt/sources.list.d/mssql-release.list  && apt-get update  && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc  && apt-get purge -y curl gnupg  && apt-get autoremove -y  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Build context is services/ so the shared module can be included.
COPY user-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/servicekit.py user-service/app.py ./

# Do not run as root inside the container.
RUN useradd --create-home --uid 10001 appuser
USER 10001

EXPOSE 5000
# gunicorn instead of Flask's development server. The access log includes the
# request ID so a request can be followed across services.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "30",      "--access-logfile", "-", "--access-logformat", "%(h)s \"%(r)s\" %(s)s %(b)s %(M)sms rid=%({x-request-id}o)s",      "app:app"]
