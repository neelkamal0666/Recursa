# Recursa Server. Configuration comes from the environment (see recursa/server/__init__.py):
#   RECURSA_DATABASE_URL          records connection (login role in recursa_app)       required
#   RECURSA_SEALED_DATABASE_URL   sealed connection (login role in recursa_gate)       defaults to the above
#   RECURSA_MIGRATE_DATABASE_URL  schema owner, for `migrate` and `keys` only          defaults to the above
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY recursa ./recursa
RUN pip install ".[server]" && rm -rf /src

RUN useradd --system --uid 10001 --no-create-home recursa
USER recursa
WORKDIR /home/recursa
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2)"
ENTRYPOINT ["recursa", "server"]
CMD ["run", "--host", "0.0.0.0", "--port", "8080"]
