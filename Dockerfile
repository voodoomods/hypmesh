# HypMesh in a container: the mesh server, plus `hypmesh seat` so an MCP client (Claude Code, Codex) can take a seat
# with `docker exec -i` and nothing installed on the host. Works with docker and podman. See docs/docker.md.
#
#   docker build -t hypmesh .
#   docker run -d --name hypmesh -p 127.0.0.1:8800:8800 -v hypmesh-data:/data \
#     -e HYPMESH_BASE_URL=http://127.0.0.1:8800 hypmesh
#
# Another port: publish the same port inside and out, e.g.
#   -p 127.0.0.1:8810:8810 -e HYPMESH_PORT=8810 -e HYPMESH_BASE_URL=http://127.0.0.1:8810
# The mesh recognises references to its own questions by HYPMESH_BASE_URL, and `hypmesh seat` inside the container
# defaults to it, so it must be the URL agents use.
FROM docker.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HYPMESH_DATA=/data \
    HYPMESH_PORT=8800

RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin hypmesh \
 && mkdir -p /data/seats && chown -R hypmesh:hypmesh /data && chmod 0700 /data

WORKDIR /src
COPY pyproject.toml README.md LICENSE DISCLAIMER.md CHANGELOG.md ./
COPY src ./src
RUN pip install ".[mcp]" && cd / && rm -rf /src

USER hypmesh
WORKDIR /data
VOLUME /data
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['HYPMESH_PORT'], timeout=3)"

CMD ["sh", "-c", "exec hypmesh --data /data serve --host 0.0.0.0 --port \"$HYPMESH_PORT\""]
