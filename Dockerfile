FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir flask osmium

COPY app.py .
COPY templates/ templates/

# search.db is NOT baked into the image — it is downloaded at pod startup
# by the init container (see k8s/deployment.yaml) and placed at /data/search.db.
# The DB_FILE environment variable overrides the default path so the app
# reads from /data/search.db instead of /app/search.db.

EXPOSE 5017

CMD ["python3", "app.py"]
