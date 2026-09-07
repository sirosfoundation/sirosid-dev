# env-admin - see server.py. Build context is the repository root, so that
# scripts/bootstrap.py (shared with `make up` and fly-up) can be copied in
# rather than duplicated:
#
#   docker build -f env-admin/Dockerfile -t sirosid-env-admin:local .
#
# Runs as root on purpose: locally it needs the mounted Docker socket to
# restart the stack's containers (the standard dev-tooling pattern). On Fly
# there is no socket and it talks to the Machines API instead.
FROM python:3.13-slim

RUN pip install --no-cache-dir --root-user-action=ignore "pymongo>=4.6,<5"

WORKDIR /app
COPY scripts/bootstrap.py /app/bootstrap.py
COPY env-admin/server.py /app/server.py

EXPOSE 3002
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:3002/health', timeout=2).status == 200 else 1)"
CMD ["python3", "/app/server.py"]
