# C/C++ Metadata Knowledge Extractor

本组件归档自 `wffx/kRepo`，现在作为主工程知识库组件的一部分维护。

它是一个面向 C/C++ 工程源码的代码知识库构建工具集。项目读取
VS Code C/C++ 插件 `vscode-cpptools` 生成的 SQLite 元数据库
`BROWSE.VC.DB`，结合源码启发式分析，围绕指定函数抽取后续生成单元测试
测试用例和 libFuzzer harness 所需的上下文知识。

在主工程架构中，本组件对应 [架构文档](../architecture.md) 中的“知识库组件”初始实现。它目前提供偏函数级的 C/C++ 元数据抽取能力，后续会逐步向平台统一知识库接口演进。

## 归档来源与裁剪

当前 `knowledge_base/src/` 下的实现来自 `https://github.com/wffx/kRepo`，最近一次同步对应提交为 `cca013f`。

归档后已按主工程目录约定做了整理：

- 代码保留在 `knowledge_base/src/`。
- 组件说明文档保留在 `docs/knowledge_base/`。
- API 文档保留在 `docs/api/knowledge_base/`。
- 测试保留在 `tests/knowledge_base/`。
- 原上游仓库的 `.git`、外层包装目录和代码目录 README 已移除。

后续裁剪原则：

- 保留知识库工作流会直接使用的能力。
- 当主工程已有等价测试后，可移除迁移初期保留的上游烟测材料。
- 优先通过平台统一知识库接口封装能力，而不是长期暴露上游目录结构。

与其他核心组件的关系：

- 为 Fuzz harness 生成 Agent 提供目标函数源码、依赖类型、子函数上下文和参数约束。
- 为 Fuzz harness 执行 Agent 提供输入约束、边界条件和可能影响种子生成的上下文。
- 为 Crash 分析 Agent 提供调用链、源码片段和参数约束，辅助判断真实缺陷或 harness 误用。
- 为调度组件提供候选入口函数、调用关系和后续可扩展的优先级信号。

## 项目目标

本组件不是某个特定 C/C++ 项目的源码仓库，也不是直接生成 harness 的最终工具。它的定位是
构建函数级知识库，为后续 libFuzzer harness 生成、种子生成和 Crash 分析提供高质量输入：

- 函数源码和依赖代码片段：宏、常量、typedef、枚举、全局变量、静态变量、
  结构体、嵌套结构体、目标函数源码和下游子函数源码。
- 符号源码片段：按宏、typedef、枚举、变量、结构体、union 等非函数符号名检索源码片段。
- 上层调用链：按 `a -> b -> target` 形式展示目标函数被哪些上层函数调用。
- 入参约束：根据函数体上下文推断参数类型、指针属性、用户态指针、范围检查、
  常量约束和关键证据行。

这些信息可用于：

- 选择适合做单元测试或 Fuzz 的入口函数。
- 构造函数参数、初始化结构体和全局上下文。
- 推断有效输入格式、边界条件和错误路径。
- 为 libFuzzer harness 生成提供种子约束和调用前置条件。

在 AxF 主流程中，这些输出会写入 `workspace/web/tasks/<task_id>/` 或 `workspace/terminal/tasks/<task_id>/`。后续任务可以通过 Web 表单的“复用知识库目录”或 Terminal CLI 的 `--knowledge-dir` 指向旧任务目录，复用已生成的 `report.json`、源码包、调用链和参数约束。复用行为由调度层完成，知识库组件本身仍只负责按命令生成产物。

## 元数据库来源

脚本依赖的 `BROWSE.VC.DB` 由 VS Code C/C++ 插件生成：

1. 安装 VS Code 扩展 `ms-vscode.cpptools`。
2. 使用 VS Code 打开待分析的 C/C++ 工程源码目录，例如 `my_project`。
3. 等待 C/C++ 插件完成 browse database 构建。
4. 默认数据库路径通常为：

```text
my_project/.vscode/BROWSE.VC.DB
```

如果数据库路径不同，可以通过 `--db` 显式指定。

`--db` 支持三种形式：

```text
path/to/BROWSE.VC.DB
path/to/.vscode
path/to/cpp-source-root
```

## 目录结构

```text
knowledge_base/
  src/                       核心实现代码
    cpp_meta_query.py        推荐 CLI 入口和 Python API re-export
    cpp_meta/                核心实现包和通用 C/C++ Python API
      base.py                  命令基类和通用配置
      models.py                SQLite 元数据模型和常量
      db.py                    SQLite 访问和函数定位
      parsing.py               源码切片、清洗和 token 提取
      dependencies.py          宏/类型/变量依赖收集和嵌套类型展开
      calls.py                 调用点解析、上游/下游调用图搜索
      params.py                入参约束推断
      engine.py                公共分析报告聚合
      filters.py               测试目录、辅助调用等候选过滤策略
      source_bundle.py         功能 1：单函数源码分析包
      call_chains.py           功能 2：上层调用链
      param_constraints.py     功能 3：入参约束
      subfunction_bundle.py    功能 4：目标函数和下游子函数分析包
      symbol_lookup.py         功能 5：非函数符号源码片段检索
      renderer.py              Markdown/.c 输出渲染
      cli.py                   argparse 命令行装配
docs/
  api/
    knowledge_base/
      cpp_meta_query.md      cpp_meta_query CLI 和 Python API 文档
      api_implementation_details.md
                             API 实现细节、符号歧义和去重策略
  knowledge_base/
    README.md                组件说明
tests/
  knowledge_base/
    test_cpp_meta_query.py   Python API 和核心能力烟测
```

本地待分析源码目录、VS Code 生成的数据库和测试生成物默认不入库，例如：

```text
linux-*/
**/.vscode/BROWSE.VC.DB
tests/fixtures/*.c
```

## 快速开始

查看主帮助：

```powershell
python .\knowledge_base\src\cpp_meta_query.py --help
```

查看某个功能接口的参数：

```powershell
python .\knowledge_base\src\cpp_meta_query.py source --help
python .\knowledge_base\src\cpp_meta_query.py subsource --help
python .\knowledge_base\src\cpp_meta_query.py calls --help
python .\knowledge_base\src\cpp_meta_query.py params --help
python .\knowledge_base\src\cpp_meta_query.py report --help
python .\knowledge_base\src\cpp_meta_query.py symbol --help
```

指定函数导出 `.c` 分析包：

```powershell
python .\knowledge_base\src\cpp_meta_query.py source parse_config --repo .\my_project --file src\config.c --output .\parse_config_bundle.c
```

查询目标函数的上层调用链：

```powershell
python .\knowledge_base\src\cpp_meta_query.py calls parse_config --repo .\my_project --file src\config.c --max-depth 3
```

推断目标函数入参约束：

```powershell
python .\knowledge_base\src\cpp_meta_query.py params parse_config --repo .\my_project --file src\config.c
```

导出目标函数及其下游子函数源码分析包：

```powershell
python .\knowledge_base\src\cpp_meta_query.py subsource parse_config --repo .\my_project --file src\config.c --max-depth 1 --output .\parse_config_subfunctions_bundle.c
```

`subsource` 默认会跳过日志、trace、debug、统计/accounting、instrumentation
等对核心控制流影响较小的辅助子函数。需要完整保留这些子函数源码时：

```powershell
python .\knowledge_base\src\cpp_meta_query.py subsource parse_config --repo .\my_project --file src\config.c --include-auxiliary-calls
```

查询非函数符号源码片段：

```powershell
python .\knowledge_base\src\cpp_meta_query.py symbol MY_MACRO --repo .\my_project --kind macro
python .\knowledge_base\src\cpp_meta_query.py symbol my_struct --repo .\my_project --kind struct --file include\types.h
```

显式指定元数据库：

```powershell
python .\knowledge_base\src\cpp_meta_query.py calls parse_config --db .\my_project\.vscode\BROWSE.VC.DB --file src\config.c
```

## 五个核心能力

### 1. source

`source` 根据函数名输出函数源码及其涉及的依赖代码片段，并合并成一个 `.c`
格式分析包。输出顺序为：

```text
常量/宏 -> typedef -> 枚举/枚举值 -> 全局变量 -> 静态变量 -> 结构体/union -> 函数
```

结构体、union、enum、typedef 中继续引用的嵌套类型会按层递归解析。默认嵌套解析层数为
`4`，可通过 `--max-nesting-depth` 调整。

### 2. subsource

`subsource` 从目标函数出发，递归解析可直接定位的下游子函数，并将目标函数、
子函数源码和这些函数共同涉及的依赖片段合并成一个 `.c` 分析包。
生成文件会保留真实目标函数和子函数源码，并在前置区域按需合成最小 typedef、宏、
结构体字段和外部调用桩，使这些源码片段能共同组成可编译的最小 C 单元。
原始数据库依赖片段默认只保留紧凑的符号、文件和行号摘要，不再把大段源码逐行注释到输出文件中。

输出仍按依赖优先组织：

```text
常量/宏 -> typedef -> 枚举/枚举值 -> 全局变量 -> 静态变量 -> 结构体/union -> 子函数/目标函数
```

函数体部分会尽量按“被调用者在前，调用者在后”排序，减少先引用后定义的情况。
对于调用规模较大的函数，可以通过 `--max-depth`、`--max-functions` 控制输出范围。
依赖片段中的嵌套类型默认递归解析 `4` 层，可通过 `--max-nesting-depth` 调整。
默认会跳过日志、trace、debug、统计/accounting、instrumentation 等辅助函数，
例如 `add_rchar`、`inc_syscr` 这类统计调用；生成文件会在
`Skipped auxiliary callees` 段落中记录跳过项。
同时会排除 `test`、`tests`、`testing`、`selftests`、`DT`、`ST` 等测试目录下的符号索引，
并对最终输出中的重复依赖和重复函数定义做去重。按需合成依赖用于语法检查，
原始依赖摘要用于知识库上下文和人工回溯。

### 3. calls

`calls` 查询目标函数被哪些上层函数调用，并以链路形式输出：

```text
upper_func -> middle_func -> target_func
```

对于调用关系较多的函数，可以通过 `--max-depth`、`--max-chains`、
`--max-callers-per-level` 控制搜索和输出规模。

#### calls 性能和 Windows 注意事项

`calls` 是当前知识抽取里最容易耗时的步骤。它需要在源码树里搜索调用点，再从数据库和源码片段中还原上层调用链。Linux 内核这类大仓库中，同名函数、宏包装和间接调用很多，搜索空间会快速变大。

实现会优先使用 `rg` 搜索源码。Web 和 Terminal 调度层在 Windows 上会预先检查 `rg`，缺失时尝试通过 Scoop 安装 `ripgrep`；直接调用 `cpp_meta_query.py` 时不会自动安装，请先确认：

```powershell
rg --version
```

Windows 上慢很多时，优先按以下顺序处理：

1. 确认 `rg` 在当前 PowerShell 中可用。
2. 把源码目录和 AxF `workspace/` 放在本地 SSD，不要放 OneDrive、网络盘或同步盘。
3. 为源码目录和 `workspace/` 配置 Defender 或第三方杀毒排除项。
4. 使用 `--file` 精确到目标文件，减少同名函数候选。
5. 降低 `--max-depth`、`--max-chains`、`--max-callers-per-level`。
6. 在完整 harness 生成链路中，如果不需要调用场景，可以先不勾选 `calls`，只用 `report_json` 和 `params` 验证模型生成质量。

性能优化的目标是缩小搜索空间，而不是把更多原始文本塞进模型上下文。`calls.txt` 是辅助上下文，不是 Harness Agent 的硬依赖。

### 4. params

`params` 根据函数源码上下文推断入参约束，包括：

- 指针参数是否被解引用。
- 是否为 `__user` 用户态指针。
- 是否经过 `access_ok`、`copy_*_user` 等检查。
- 参数是否参与大小、范围、flag、NULL 判断。
- 相关源码证据行。

### 5. symbol

`symbol` 根据非函数符号名检索源码片段，并直接打印到终端，不写文件。适用于宏定义、
typedef、枚举、枚举值、全局变量、静态变量、结构体和 union 等符号。

```powershell
python .\knowledge_base\src\cpp_meta_query.py symbol MY_MACRO --repo .\my_project --kind macro
```

如果同名符号在数据库中出现多次，`symbol` 会按候选顺序打印多个片段，可通过
`--kind` 和 `--file` 收窄范围。

## Python API

核心能力也可以作为 Python API 使用：

```python
from src.cpp_meta_query import (
    export_source_bundle,
    export_subfunction_source_bundle,
    lookup_symbol_source,
    print_function_call_sequence,
    print_function_param_constraints,
    print_symbol_source,
)

export_source_bundle(
    "parse_config",
    output="parse_config_bundle.c",
    repo=r".\my_project",
    file_filter=r"src\config.c",
)

export_subfunction_source_bundle(
    "parse_config",
    output="parse_config_subfunctions_bundle.c",
    repo=r".\my_project",
    file_filter=r"src\config.c",
    max_depth=1,
)

print_function_call_sequence(
    "parse_config",
    repo=r".\my_project",
    file_filter=r"src\config.c",
)

print_function_param_constraints(
    "parse_config",
    repo=r".\my_project",
    file_filter=r"src\config.c",
)

symbol_report = lookup_symbol_source(
    "MY_MACRO",
    repo=r".\my_project",
    kind="macro",
)
```

## 验证

运行单元测试：

```powershell
python -m unittest tests.knowledge_base.test_cpp_meta_query -v
```

当前测试覆盖：

- 函数定位和源码读取。
- `.c` 分析包导出。
- 目标函数和下游子函数 `.c` 分析包导出。
- 非函数符号源码片段检索。
- 嵌套结构体递归解析。
- 测试目录过滤、辅助调用过滤和重复定义去重。
- 上层调用链输出。
- 入参约束输出。

## 已知边界

- `BROWSE.VC.DB` 中部分符号关系表可能为空，因此调用链和参数约束包含源码
  启发式分析结果，不等价于完整编译器级调用图。
- `source` 输出的 `.c` 文件是知识库分析包，便于阅读和后续处理，不保证可直接
  作为独立 C/C++ 编译单元编译。
- 同名函数较多时建议使用 `--file` 指定源码路径子串。
- 大型源码树和 `BROWSE.VC.DB` 体积较大，默认由 `.gitignore` 排除。

## Linux 验证样例

本仓库的本地测试使用 Linux 源码树作为大型 C 工程验证样例，例如：

```powershell
python .\knowledge_base\src\cpp_meta_query.py source vfs_read --repo .\linux-7.0 --file fs\read_write.c
python .\knowledge_base\src\cpp_meta_query.py calls can_send --repo .\linux-7.0 --file net\can\af_can.c
```

这些案例用于验证工具可处理大型 C 工程，并不代表工具只支持 Linux。

更多命令、参数和接口说明见
[cpp_meta_query API 文档](../api/knowledge_base/cpp_meta_query.md)；实现细节、符号歧义和去重策略见
[cpp_meta_query API 实现细节](../api/knowledge_base/api_implementation_details.md)。
