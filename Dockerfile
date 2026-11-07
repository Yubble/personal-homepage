# 个人主页服务镜像
# 注意：服务器部署时整个仓库目录会挂载进 /app（见 docker-compose.yml），
# 这里的 COPY 只是让镜像自身也能独立运行（不挂载也能起）。
FROM python:3.12-slim

WORKDIR /app

# git：发布接口要在容器内对挂载进来的宿主仓库做提交
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY site/ ./site/

ENV PYTHONUNBUFFERED=1
# 挂载进来的宿主仓库属主是部署用户而非 root，git 默认拒绝操作——显式豁免
RUN git config --global --add safe.directory /app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
