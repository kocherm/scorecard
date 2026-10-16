FROM python:3.12-slim

WORKDIR /srv/scorecard
# requirements.txt is GENERATED from uv.lock (exact == pins, transitive deps
# included) so the image runs the versions the tests ran on. After changing
# dependencies: uv lock && uv export --frozen --no-dev --no-hashes \
#   --no-emit-project --format requirements-txt -o requirements.txt
# --no-deps: the file is the complete set; pip must not resolve anything else.
COPY requirements.txt .
RUN pip install --no-cache-dir --no-deps -r requirements.txt

COPY app ./app
COPY migrate ./migrate

ENV SCORECARD_DB=/srv/scorecard/data/scorecard.db \
    PYTHONUNBUFFERED=1

# Which peers uvicorn believes X-Forwarded-For/-Proto from. "*" keeps the
# out-of-the-box behaviour; a deployment should set its reverse proxy's
# address here in a local compose override (environment: FORWARDED_ALLOW_IPS),
# or any client that reaches the port directly can claim to be anyone.
ENV FORWARDED_ALLOW_IPS="*"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
