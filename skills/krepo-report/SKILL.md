# krepo-report

## 目标

复用 kRepo 或 AxF 内置 knowledge_base，为目标函数生成函数级 JSON 和 Markdown 报告。

## 输入

- `repo`
- `function`
- `file`
- `db`
- `artifact_dir`
- 可选 `krepo_root` / `krepo_provider`

## 输出

- `report_json`
- `report_md`

## 约束

- 输出必须写入 AxF run 的 `artifacts/krepo/`。
- 不读取或复用 kRepo 的调度状态。
- 不修改 kRepo 来源代码。
