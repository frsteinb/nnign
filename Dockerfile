FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Europe/Berlin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Der Dienst läuft als unprivilegierter Benutzer; das Entrypoint-Skript wechselt nach dem
# Anpassen der Rechte am Datenverzeichnis dorthin.
RUN groupadd --system --gid 10001 nnign \
    && useradd --system --uid 10001 --gid 10001 --home /app nnign \
    && mkdir -p /data && chown nnign:nnign /data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
