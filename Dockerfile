FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home bridge && mkdir /data && chown bridge:bridge /data
COPY bridge ./bridge
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "bridge.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
