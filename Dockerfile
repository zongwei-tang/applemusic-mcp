# Cloud image: a library-and-playlist MCP over Streamable HTTP.
#
# No Music.app, Safari or Chrome in a container, so it runs at --level api with
# the playback and queue tools hidden: catalog search, library, playlists and
# discovery only. Needs two secrets at runtime, both set in the host's secret
# store, never baked into the image:
#   APPLEMUSIC_MCP_TOKEN   bearer token clients send (>= 32 chars, `openssl rand -hex 32`)
#   APPLEMUSIC_USER_TOKEN  your Apple Music media-user-token (`applemusic-mcp export-token`)
# The developer token is fetched from music.apple.com on demand.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APPLEMUSIC_NO_KEYRING=1 \
    APPLEMUSIC_MCP_HOME=/data \
    APPLEMUSIC_LEVEL=api \
    APPLEMUSIC_NO_PLAYBACK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app /data

USER app
EXPOSE 8770

# --port defaults to $PORT, which the platform sets.
CMD ["applemusic-mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--level", "api", "--no-playback"]
