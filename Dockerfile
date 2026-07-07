FROM python:3.12-slim

WORKDIR /srv/scorecard
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrate ./migrate

ENV SCORECARD_DB=/srv/scorecard/data/scorecard.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
