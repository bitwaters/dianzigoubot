FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY config/ config/
COPY plugins/ plugins/

CMD ["python", "-m", "app.core.main"]
