# The scorer builds this on linux/amd64 and runs it with no network, read-only,
# as user nobody. The base is the image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The packages the commits were labeled with, from PyPI.
COPY requirements.lock /opt/requirements.lock
RUN pip install --no-cache-dir -r /opt/requirements.lock

# sqlglot at the pinned commit, from its own repository on GitHub, with its
# bytecode compiled now because the disk is read-only at run time.
COPY tools/fetch_sqlglot.py /opt/fetch_sqlglot.py
RUN python /opt/fetch_sqlglot.py /sqlglot \
    && (python -m compileall -q /sqlglot/sqlglot /sqlglot/tests || echo "compileall: some files left to compile at run time") \
    && chmod -R a+rX /sqlglot

COPY blast /app/blast
COPY data/map.json /app/data/map.json
COPY data/imports.json /app/data/imports.json
COPY selector.py /app/selector.py
RUN python -m compileall -q /app && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    SKIP_INTEGRATION=0 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    PYTHONPATH=/sqlglot
CMD ["python", "/app/selector.py"]
