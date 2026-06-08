# krepo-subsource

## 目标

复用 kRepo 下游子函数源码包能力，导出目标函数和下游子函数分析包。

## 输入

- `repo`
- `function`
- `file`
- `db`
- `artifact_dir`
- 可选 `max_depth`
- 可选 `max_functions`

## 输出

- `subsource_c`

## 约束

- 输出必须写入 AxF run 的 `artifacts/krepo/subsource.c`。
