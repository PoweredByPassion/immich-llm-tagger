# syntax=docker/dockerfile:1
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl make tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# 1) 构建上下文（_immich_gen 与 tailwind.css 已被 .dockerignore 排除，
#    必须在镜像内生成）
COPY pyproject.toml ./
COPY app/ app/
COPY vendor/ vendor/
COPY Makefile tailwind.config.js ./

# 2) 仅构建期使用的代码生成器，不进入运行时依赖。
#    --mount=type=cache：wheel 缓存持久在 BuildKit 缓存卷，重复 build 不重新下载。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install openapi-python-client

# 3) 容器内无 .venv，必须覆盖 Makefile 的默认解释器
RUN make generate PYTHON=python

# 4) 安装为非 editable 包，使 _immich_gen 与模板/静态文件随包分发。
#    依赖来自 pyproject；代码层在上一步已 COPY，代码改动时此层重建但依赖
#    仍由缓存卷供给，不重新下载。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

# 5) Tailwind 产物
#    --mount=type=cache：tailwind 二进制缓存在 BuildKit 缓存卷，重复 build 不重新下载；
#    二进制只在构建期使用，不进入最终镜像层。
RUN --mount=type=cache,target=/root/.cache/tailwind \
    make css TAILWIND_CACHE_DIR=/root/.cache/tailwind

ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/api/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
