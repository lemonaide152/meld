FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/lemonaide152/meld"
LABEL org.opencontainers.image.title="meld"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
ENV PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
