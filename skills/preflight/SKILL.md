# preflight

## 目标

检查 AxF workflow 运行所需的基础输入和本地路径。

## 输入

- `repo`：源码根目录，例如 `/linux-7.0`。
- `function`：目标函数名。
- `file`：可选源码文件过滤。
- `db`：可选 `BROWSE.VC.DB` 路径。

## 输出

- 归一化后的 repo、function、file、db。
- repo 和 db 是否存在。

## 约束

- 不修改源码树。
- 不写入 kRepo。
