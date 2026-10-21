FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
ENV SA_MODE=server SA_HOST=0.0.0.0 SA_PORT=8766 SA_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8766/healthz',timeout=4)" || exit 1
CMD ["python3", "app.py"]
