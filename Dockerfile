# Scorer builds on linux/amd64, runs with no network as user nobody. Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The environment the commits were labeled in, so the selector can run candidate tests against a patched tree.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.lock
# The pinned sqlglot checkout (source, tests and fixtures), from sqlglot's own repository.
RUN python - <<'PY'
import io, tarfile, urllib.request
sha = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz", timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    members = [m for m in tar.getmembers() if "/" in m.name]
    for m in members:
        m.name = m.name.split("/", 1)[1]
    tar.extractall("/opt/sqlglot", members=members, filter="data")
PY
COPY selector.py map.bin /app/
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests /app && chmod -R a+rX /opt/sqlglot /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
