FROM python:3.12-slim

# Bring in the method author's code at a pinned ref, never a branch, e.g.:
#   ARG METHOD_REF=<commit sha or release tag>
#   RUN apt-get update && apt-get install -y --no-install-recommends git \
#    && git clone https://github.com/<author>/<method>.git /opt/method \
#    && git -C /opt/method checkout "${METHOD_REF}" \
#    && rm -rf /var/lib/apt/lists/*
# Do not copy model weights into the image; download them at runtime into the cache
# path described in the README.

# Not /code: the backend mounts its own config files under /code/app/cfg in the job container
WORKDIR /opt/job

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wrapper/ wrapper/

ENTRYPOINT ["python", "-m", "wrapper.entrypoint"]
