# The scorer builds this on linux/amd64 and runs it with no network, read-only,
# as user nobody. The base is the image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit, from its own repository on GitHub.
COPY tools/fetch_sqlglot.py /opt/fetch_sqlglot.py
RUN python /opt/fetch_sqlglot.py /sqlglot && chmod -R a+rX /sqlglot

COPY blast /app/blast
COPY data/map.json /app/data/map.json
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
