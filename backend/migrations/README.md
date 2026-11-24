初始结构由 `src/models/tables.py` 定义，在 API 启动前由 `metadata.create_all` 创建。演示数据通过独立入口填充。

`merge_ledgers` 表记录各合并数据版本的同版冲突隔离状态（`meta.quarantined`），随首次合并更正由 `create_all` 自动建立，无需对既有库执行 ALTER。
