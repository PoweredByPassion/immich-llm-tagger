# 解释器可覆盖：容器里没有 .venv，用 `make PYTHON=python generate`（generate 依赖
# 全局安装的 openapi-python-client；本机开发则用默认的 .venv/bin/python）。
PYTHON ?= .venv/bin/python

SPEC := vendor/immich-openapi-v3.3.1.json
IMMICH_GEN := app/clients/_immich_gen

generate: $(SPEC)
	rm -rf $(IMMICH_GEN)
	$(PYTHON) -m openapi_python_client generate \
	  --path $(SPEC) --output-path $(IMMICH_GEN) --meta none --overwrite
	touch $(IMMICH_GEN)/__init__.py

TAILWIND_VERSION := v3.4.17
# 二进制缓存目录：本地开发用 ./bin；Docker 构建通过 TAILWIND_CACHE_DIR 覆盖为
# --mount=type=cache 挂载点，重复 build 命中缓存不再下载。
# 文件名带版本号：升级 TAILWIND_VERSION 会拉取新文件，旧版本缓存仍可复用。
TAILWIND_CACHE_DIR ?= ./bin
TAILWIND_BIN := $(TAILWIND_CACHE_DIR)/tailwindcss-$(TAILWIND_VERSION)

css: $(TAILWIND_BIN)
	$(TAILWIND_BIN) -i app/web/static/input.css -o app/web/static/tailwind.css --minify

$(TAILWIND_BIN):
	mkdir -p $(TAILWIND_CACHE_DIR)
	curl -fsSL -o $@.tmp \
	  https://github.com/tailwindlabs/tailwindcss/releases/download/$(TAILWIND_VERSION)/tailwindcss-linux-x64
	mv $@.tmp $@
	chmod +x $@

.PHONY: generate css
