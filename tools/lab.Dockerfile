# Offline lab image: the environment the commits were labeled in, plus git and
# coverage. It builds the maps and labels calibration patches. It is never
# submitted; the scorer builds the Dockerfile at the top of the repository.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY tools/fetch_sqlglot.py /opt/fetch_sqlglot.py
RUN python /opt/fetch_sqlglot.py /sqlglot
COPY requirements.lock /opt/requirements.lock
RUN pip install --no-cache-dir -r /opt/requirements.lock coverage==7.11.0
ENV PYTHONPATH=/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /sqlglot
