FROM python:3.12-slim

ARG PUZZARY_VERSION

LABEL org.opencontainers.image.title="Puzzary" \
      org.opencontainers.image.description="Self-hosted extensible puzzle and game library" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${PUZZARY_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY LICENSE NOTICE /licenses/puzzary/
COPY VERSION ./VERSION

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY app ./app

EXPOSE 8790

CMD ["python", "-m", "app.server"]
