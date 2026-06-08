# krepo-calls

## 目标

复用 kRepo 上游调用链分析能力，为目标函数生成调用链文本。

## 输入

- `repo`
- `function`
- `file`
- `db`
- `artifact_dir`
- 可选 `call_depth`

## 输出

- `calls_text`

## 约束

- 输出必须写入 AxF run 的 `artifacts/krepo/calls.txt`。
