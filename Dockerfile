# The server speaks MCP over stdio, so run it with -i: docker run -i --rm matchawards-mcp
# HTTP mode: docker run --rm -p 127.0.0.1:8765:8765 -e MATCHAWARDS_HTTP_HOST=0.0.0.0 matchawards-mcp matchawards-mcp --http
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
CMD ["matchawards-mcp"]
