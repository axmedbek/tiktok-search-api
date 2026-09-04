FROM python:3.11-slim

WORKDIR /app

# Dependencies first so the layer is cached across code changes.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# api_signed.py does sys.path.insert(0, dirname(abspath(__file__))) -> /app,
# so the package must sit at /app/tiktoksearch for `from tiktoksearch.api
# import create_app` to resolve.
COPY mobile/tiktoksearch /app/tiktoksearch
COPY mobile/api_signed.py /app/api_signed.py

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Config profile and warm identities are bind-mounted read-only at /app/config;
# nothing secret is baked into the image (see .dockerignore).
ENV TTAPI_SIGNED_CONFIG=/app/config/config_direct.yaml
ENV TIKTOK_IDENTITIES_PATH=/app/config/identities.json
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# python:3.11-slim ships no curl/wget, so probe with stdlib urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).read()"]

# No --config flag on purpose: argparse defaults it to _CONFIG, so
# args.config == _CONFIG and main() reuses the module-level app instead of
# calling create_app a second time (which would build the pool twice).
CMD ["python", "api_signed.py", "--host", "0.0.0.0", "--port", "8000"]
