FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PORT=8000
EXPOSE 8000

# Refresh-token flow only — no browser needed in the container
ENV GOOGLE_ADS_USE_PROTO_PLUS=true

CMD ["python", "server.py"]
