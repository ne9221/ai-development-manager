FROM python:3.12-slim
WORKDIR /app
COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt
COPY . .
ENV PYTHONUNBUFFERED=1 PORT=8080
CMD ["uvicorn", "cloud.asgi:app", "--host", "0.0.0.0", "--port", "8080"]
