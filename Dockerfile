FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/lemonaide152/meld"
LABEL org.opencontainers.image.title="meld"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py meld_app.py meld_store.py meld_spec.py meld_docs.py meld_ui.py ./
ENV PORT=8080
EXPOSE 8080
USER nobody
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "server.py"]
