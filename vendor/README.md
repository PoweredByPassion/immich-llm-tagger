# vendor/ — 第三方产物

## `immich-openapi-v3.3.1.json`

Immich 官方 OpenAPI 规范，用于 `make generate` 生成客户端（`app/clients/_immich_gen/`，
gitignored）。**不要手改此文件**；升级版本时整份替换并同步更新本 README 与 `Makefile` 里的 `SPEC`。

| 项 | 值 |
|----|----|
| 来源 | <https://raw.githubusercontent.com/immich-app/immich/v3.3.1/open-api/immich-openapi-specs.json> |
| Immich 版本 | v3.3.1（`info.version` = `3.3.1`） |
| 抓取日期 | 2026-10-10 |
| 文件大小 | 953386 bytes |
| sha256 | `489ad9bf7120d97bcf2edac2f5ca461aaca0826e9165554ac717c39fa9621c04` |
| 规模 | 193 paths / 438 schemas |

校验：

```bash
sha256sum vendor/immich-openapi-v3.3.1.json
# 489ad9bf7120d97bcf2edac2f5ca461aaca0826e9165554ac717c39fa9621c04
```
