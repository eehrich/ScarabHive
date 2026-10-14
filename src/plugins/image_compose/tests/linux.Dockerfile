# The canvas layer on Linux: Debian's Chromium, run as root with --no-sandbox
# -- the way a server runs it. The tests here measure the walls around the
# page (network, files, downloads, the timeout kill); run them on Linux too
# after changing those. The context is the committed tree without the secrets:
#
#   git archive --format=tar HEAD -- . ':(exclude)config/secrets.env' \
#     | ssh root@<docker host> docker build -t canvas-test -f src/plugins/image_compose/tests/linux.Dockerfile -
#   ssh root@<docker host> docker run --rm --init canvas-test
#
# --init: without it the container's first process is Python, and the browser's
# children stay behind as zombies -- dead, but never reaped.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-dejavu-core procps \
 && rm -rf /var/lib/apt/lists/* \
 && echo 'export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-sandbox"' > /etc/chromium.d/zz-no-sandbox \
 && mkdir -p /root/Downloads

WORKDIR /app
COPY requirements/core.txt /tmp/core.txt
RUN pip install --no-cache-dir -r /tmp/core.txt numpy svglib reportlab pytest-timeout
COPY . /app

ENV PYTHONPATH=/app/src:/app/tests
CMD ["python", "-m", "pytest", "src/plugins/image_compose/tests/test_canvas_layer.py", "-q", "-p", "no:cacheprovider"]
