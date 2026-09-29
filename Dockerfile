# 生产镜像：最小基底 + 纯 Dockerfile / docker-compose 构建
#
# 基底 python:3.13-slim（debian:trixie-slim），与 M0 已验证通过的 Debian chromium 150 同源。
# 本机网络下 docker.io 直连与公共加速站均不可用，基底需先经 public.ecr.aws 拉取后本地打 tag：
#   docker pull public.ecr.aws/docker/library/python:3.13-slim
#   docker tag  public.ecr.aws/docker/library/python:3.13-slim python:3.13-slim
# 能直连 docker.io 的机器无需此步骤，FROM 语句照常生效。

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    CHROMIUM_BIN=/usr/bin/chromium \
    TZ=Asia/Hong_Kong

# Debian 默认源（deb.debian.org）本机直连不通，换清华源。
# 浏览器用 apt 装的 chromium：Playwright 官方 CDN 与 npmmirror 均不可用，无法用其自带浏览器。
RUN printf 'deb https://mirrors.tuna.tsinghua.edu.cn/debian/ trixie main contrib non-free-firmware\ndeb https://mirrors.tuna.tsinghua.edu.cn/debian/ trixie-updates main contrib non-free-firmware\n' > /etc/apt/sources.list.d/tuna.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && apt-get update -qq \
    && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

WORKDIR /app
COPY skus_hk.json main.py ./

CMD ["python3", "main.py"]
