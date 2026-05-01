产品化赛道配置目录（`tracks/`）

- 这里用于放置应用实际使用的赛道配置 JSON（起终线、方向等）。
- 真实赛道配置（`*.json`）默认不入库，已在 `.gitignore` 忽略。
- 仓库中仅保留模板文件（`*.template.json`）用于结构说明和开发联调。

建议流程：

1. 复制模板：`tracks/track.template.json` -> `tracks/<赛道名>.json`
2. 填写赛道信息与 `label_info.line_lonlat`
3. 在应用“切圈 -> 选择赛道”流程中选择该 JSON 使用

