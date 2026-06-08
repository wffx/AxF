# fuzz-harness-gen

## 目标

复用 AxF 现有 Harness Generation Agent，基于目标函数和 kRepo 辅助上下文生成 libFuzzer harness，并完成编译、修复和短跑。

## 输入

- `repo`
- `function`
- `file`
- `artifact_dir`
- `run_dir`
- 可选模型和 clang 配置。

## 输出

- `harness_dir`
- `generated_harness`
- `harness_spec`

## 约束

- 只写入当前 workflow run 目录。
- 不修改 Linux 源码或 kRepo 来源代码。
