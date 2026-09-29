FROM docker.io/library/python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
ARG SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.source="https://github.com/SnowballSH/OpenBench" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      org.opencontainers.image.version="${SOURCE_COMMIT}" \
      org.opencontainers.image.title="openbench" \
      org.opencontainers.image.description="OpenBench chess engine testing framework (SnowballSH fork)" \
      org.opencontainers.image.documentation="https://github.com/SnowballSH/OpenBench/blob/master/docs/DEPLOYMENT.md" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENBENCH_DATA_DIR=/data \
    OPENBENCH_UPLOAD_TEMP_DIR=/data/upload-tmp
RUN groupadd --system --gid 10001 openbench \
 && useradd --system --uid 10001 --gid openbench --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin openbench
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY . .
RUN OPENBENCH_SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput \
 && python -m compileall -q /app \
 && install -d -o openbench -g openbench -m 0700 /data
VOLUME /data
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/container-entrypoint.sh"]
