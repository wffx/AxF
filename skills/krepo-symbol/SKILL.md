# krepo-symbol

## 目标

复用 kRepo 符号查询能力，查询宏、typedef、enum、struct、union、变量等源码片段。

## 输入

- `repo`
- `symbol`
- `kind`
- `file`
- `db`
- `artifact_dir`

## 输出

- `symbol_text`

## 约束

- 输出必须写入 AxF run 的 `artifacts/krepo/symbols/`。
- 第一阶段 workflow 暂不默认启用。
