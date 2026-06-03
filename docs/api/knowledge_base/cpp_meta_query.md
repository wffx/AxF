# cpp_meta_query API 文档

`cpp_meta_query.py` 读取 VS Code C/C++ 插件生成的
`BROWSE.VC.DB`，按函数名或符号名查询 C/C++ 工程源码元数据，并提供 5 个独立接口。

符号解析、同名候选选择、下游子函数去重和调用链歧义等实现细节见
`api_implementation_details.md`。本文档侧重命令行接口、Python API 和验证案例；组件定位和集成关系见 [知识库组件说明](../../knowledge_base/README.md)。

默认源码根目录：

```text
.
```

默认数据库路径：

```text
./.vscode/BROWSE.VC.DB
```

如果待分析工程不在当前目录，可通过 `--repo` 或 `--db` 指定。

## 代码结构

当前实现已拆分为可扩展的包结构：

```text
knowledge_base/src/cpp_meta_query.py      CLI 入口和 Python API re-export
knowledge_base/src/cpp_meta/__init__.py   通用 C/C++ Python API
knowledge_base/src/cpp_meta/base.py       命令基类和通用配置
knowledge_base/src/cpp_meta/models.py     SQLite 元数据模型和常量
knowledge_base/src/cpp_meta/db.py         SQLite 访问和函数定位
knowledge_base/src/cpp_meta/parsing.py    源码切片、清洗和 token 提取
knowledge_base/src/cpp_meta/dependencies.py
                             宏/类型/变量依赖收集和嵌套类型展开
knowledge_base/src/cpp_meta/calls.py      调用点解析、上游/下游调用图搜索
knowledge_base/src/cpp_meta/params.py     入参约束推断
knowledge_base/src/cpp_meta/engine.py     公共分析报告聚合
knowledge_base/src/cpp_meta/source_bundle.py
knowledge_base/src/cpp_meta/call_chains.py
knowledge_base/src/cpp_meta/param_constraints.py
knowledge_base/src/cpp_meta/subfunction_bundle.py
knowledge_base/src/cpp_meta/renderer.py
knowledge_base/src/cpp_meta/symbol_lookup.py
knowledge_base/src/cpp_meta/cli.py
```

五个功能分别拆在独立模块中，后续新增知识库构建能力时，优先新增新的 command
模块并复用 `CppMetaCommand` 基类。

## 查看终端帮助

主帮助会直接打印所有通用配置项、子命令专用配置项和常用示例：

```powershell
python .\knowledge_base\src\cpp_meta_query.py --help
```

查看某个功能接口的完整参数和默认值：

```powershell
python .\knowledge_base\src\cpp_meta_query.py source --help
python .\knowledge_base\src\cpp_meta_query.py subsource --help
python .\knowledge_base\src\cpp_meta_query.py calls --help
python .\knowledge_base\src\cpp_meta_query.py params --help
python .\knowledge_base\src\cpp_meta_query.py report --help
python .\knowledge_base\src\cpp_meta_query.py symbol --help
```

`--help` 中重点配置项包括：

```text
--repo PATH
  C/C++ 源码根目录，默认当前目录 `.`。

--db PATH
  元数据库选择器。可以直接传 BROWSE.VC.DB 文件、.vscode 目录，
  或包含 .vscode/BROWSE.VC.DB 的源码根目录。

--file TEXT
  源文件路径子串，用来在同名函数中选择目标定义。

--max-deps N
  每类依赖最多采集多少项。普通功能默认 20，source 默认 200，
  subsource 默认 500。

--max-candidates N
  同名函数候选最多检查多少项，默认 12。

--max-snippet-lines N
  每个依赖源码片段最多输出多少行，默认 80。

--max-nesting-depth N
  source/subsource 的嵌套结构体、union、enum、typedef 递归解析层数，
  默认 4 层。

--kind KIND
  symbol 子命令使用，用于限定非函数符号类别。可选 macro、macro_define、
  typedef、enum、enumerator、variable、struct、union。
```

## 五个功能接口

脚本同时支持命令行接口和 Python import 接口。命令行接口如下。

### 1. source：输出源码片段并合并成 .c 文件

输入函数名，输出函数源码，以及源码涉及的结构体、typedef、枚举/枚举值、常量/宏、全局变量、静态变量等代码片段，合并为一个 `.c` 文件。对于结构体、union、enum、typedef 中继续引用的嵌套类型，脚本会递归解析并一并写入 `.c` 文件。默认嵌套解析层数为 `4`。

```powershell
python .\knowledge_base\src\cpp_meta_query.py source parse_config --repo .\my_project --file src\config.c --output .\parse_config_bundle.c
```

嵌套类型递归深度默认是 `4` 层，可以调整：

```powershell
python .\knowledge_base\src\cpp_meta_query.py source parse_config --repo .\my_project --file src\config.c --max-nesting-depth 6
```

如果不指定 `--output`，默认输出：

```text
<函数名>_source_bundle.c
```

例如：

```powershell
python .\knowledge_base\src\cpp_meta_query.py source parse_config --repo .\my_project --file src\config.c
```

会生成：

```text
parse_config_source_bundle.c
```

生成的 `.c` 文件是分析包，便于阅读和后续处理，不保证能直接作为独立 C/C++ 编译单元编译。

### 2. subsource：输出目标函数及下游子函数源码分析包

输入函数名，递归解析目标函数调用到的下游子函数，输出目标函数、子函数源码，
以及这些源码共同涉及的结构体、typedef、枚举/枚举值、常量/宏、全局变量、
静态变量等代码片段，并合并为一个 `.c` 文件。

`subsource` 生成文件会保留真实目标函数和子函数源码，并在前置区域按需合成最小
typedef、宏、结构体字段和外部调用桩，使这些源码片段能共同组成可编译的最小 C 单元。
原始数据库依赖片段默认只保留紧凑的符号、文件和行号摘要，不再把大段源码逐行注释到输出文件中。

```powershell
python .\knowledge_base\src\cpp_meta_query.py subsource parse_config --repo .\my_project --file src\config.c --max-depth 1 --output .\parse_config_subfunctions_bundle.c
```

输出文件会按如下顺序组织，减少先引用后定义的情况：

```text
常量/宏 -> typedef -> 枚举/枚举值 -> 全局变量 -> 静态变量 -> 结构体/union -> 子函数/目标函数
```

函数体部分会尽量按“被调用者在前，调用者在后”排序。结构体、union、enum、typedef
中的嵌套类型会递归解析并一并写入 `.c` 文件，默认递归解析 `4` 层。

常用限制参数：

```powershell
python .\knowledge_base\src\cpp_meta_query.py subsource parse_config --repo .\my_project --file src\config.c --max-depth 2 --max-functions 50 --max-nesting-depth 4
```

`subsource` 目前解析普通直接函数调用。对于函数指针调用和成员调用，例如
`file->f_op->read()`，脚本会保留原函数源码，但不会强行解析到某个全局函数实现。

默认情况下，`subsource` 会跳过日志、trace、debug、统计/accounting、
instrumentation 等辅助子函数，避免把不影响核心逻辑的函数也展开进分析包。
例如统计计数或日志函数会保留在原函数
源码里，但不会额外展开它们的函数体。生成文件会在 `Skipped auxiliary callees`
段落中记录跳过项。

`subsource` 还会默认排除 `test`、`tests`、`testing`、`selftests`、`DT`、`ST`
等测试目录下的符号索引，避免 cpptools 数据库中的测试代码同名符号被合并进分析包，
造成宏、枚举、结构体或函数的重定义。
最终 `.c` 输出还会按符号类别和名称去重；多个下游子函数共享同一个宏、typedef、
枚举、全局变量、静态变量、结构体或函数实现时，只保留一份定义。

如果需要完整保留这些辅助函数源码，可加：

```powershell
python .\knowledge_base\src\cpp_meta_query.py subsource parse_config --repo .\my_project --file src\config.c --include-auxiliary-calls
```

### 3. calls：输出上层调用链路

输入函数名，递归查找“哪些上层函数调用了目标函数”，并用 `a -> b -> 目标函数` 的形式在命令行打印完整链路。

```powershell
python .\knowledge_base\src\cpp_meta_query.py calls parse_config --repo .\my_project --file src\config.c --max-depth 3
```

`calls` 会优先使用 `rg` 搜索调用点。直接运行知识库 CLI 时不会自动安装 `ripgrep`，Windows 上建议先确认：

```powershell
rg --version
```

如果没有 `rg`，调用链搜索会慢很多。Web 和 Terminal 入口会在 Windows 任务开始前自动检查并尝试安装 `ripgrep`；直接调用本脚本时需要用户自己准备环境。

输出示例：

```text
Target: parse_config (MY_PROJECT\SRC\CONFIG.C:120-180)
1. load_config -> parse_config
   load_config@MY_PROJECT\SRC\CONFIG.C:210
```

### 4. params：输出函数入参约束和格式

输入函数名，根据函数上下文在命令行打印入参类型、格式、约束和证据行。

```powershell
python .\knowledge_base\src\cpp_meta_query.py params parse_config --repo .\my_project --file src\config.c
```

输出示例：

```text
Parameter: buf
Type/format: char __user *buf
Constraints:
- 指针参数；需要调用者提供有效地址，除非函数显式允许 NULL。
- 用户态指针；通常需要配合 access_ok/copy_*_user 等检查。
- 通过 access_ok 校验可访问范围。
Evidence:
- 562:     if (unlikely(!access_ok(buf, count)))
```

### 5. symbol：输出非函数符号源码片段

输入符号名，检索宏定义、typedef、枚举、枚举值、变量、结构体或 union 等非函数符号，
并在标准输出中打印匹配到的源码片段。该接口不写输出文件。

```powershell
python .\knowledge_base\src\cpp_meta_query.py symbol MY_MACRO --repo .\my_project --kind macro
```

如果同名符号在数据库中出现多次，`symbol` 会打印多个候选，数量由 `--max-candidates`
控制。可用 `--kind` 限定符号类别，用 `--file` 限定源码路径子串：

```powershell
python .\knowledge_base\src\cpp_meta_query.py symbol my_struct --repo .\my_project --kind struct --file include\types.h
```

输出示例：

````text
Symbol: MY_MACRO
Kind filter: macro
Candidates: 1 shown / 1 total

[1] macro_define MY_MACRO (MY_PROJECT\INCLUDE\CONFIG.H:12-12)
```c
#define MY_MACRO 1
```
````

## 完整报告接口

`report` 是 `source`、`calls`、`params`、`subsource` 四个能力的统一汇总：

- 源码和依赖片段使用与 `source` 一致的嵌套类型解析，默认递归 `4` 层。
- 调用关系使用与 `calls` 一致的上层调用链，按 `a -> b -> target` 输出。
- 入参约束使用与 `params` 一致的推断逻辑。
- 下游子函数使用与 `subsource` 一致的默认分析范围，包含函数列表、跳过的辅助调用和限制信息。
- 下游子函数分析同样排除 `test/tests/testing/selftests/DT/ST` 等测试目录中的符号索引。
- 为避免报告过长，`report` 会压缩长代码片段：目标函数源码、依赖片段或下游子函数源码过长时，
  报告保留文件路径和行号，不再内联整段代码。

```powershell
python .\knowledge_base\src\cpp_meta_query.py report parse_config --repo .\my_project --file src\config.c
```

完整报告也支持 JSON：

```powershell
python .\knowledge_base\src\cpp_meta_query.py report parse_config --repo .\my_project --file src\config.c --format json
```

## Python API 用法

也可以在其他 Python 脚本中直接 import 五个接口：

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
    max_nesting_depth=4,
)

export_subfunction_source_bundle(
    "parse_config",
    output="parse_config_subfunctions_bundle.c",
    repo=r".\my_project",
    file_filter=r"src\config.c",
    max_depth=1,
    max_functions=20,
    max_nesting_depth=4,
)

print_function_call_sequence(
    "parse_config",
    repo=r".\my_project",
    file_filter=r"src\config.c",
    max_depth=3,
)

print_function_param_constraints(
    "parse_config",
    repo=r".\my_project",
    file_filter=r"src\config.c",
)

print_symbol_source(
    "MY_MACRO",
    repo=r".\my_project",
    kind="macro",
)

symbol_report = lookup_symbol_source(
    "my_struct",
    repo=r".\my_project",
    kind="struct",
)
```

## 通用参数

这些参数也会直接显示在终端帮助中：

```powershell
python .\knowledge_base\src\cpp_meta_query.py --help
python .\knowledge_base\src\cpp_meta_query.py source --help
```

`function` / `symbol`
: 必填，待查询函数名或非函数符号名，例如 `parse_config`、`decode_packet`、`vfs_read`、`MY_MACRO`。

`--repo`
: C/C++ 源码根目录，默认当前目录 `.`。

`--db`
: SQLite 元数据库路径。支持直接传 `BROWSE.VC.DB` 文件、包含该文件的 `.vscode` 目录，或包含 `.vscode/BROWSE.VC.DB` 的源码根目录。指定 DB 后脚本会尽量自动推断源码根目录。

示例：

```powershell
python .\knowledge_base\src\cpp_meta_query.py calls parse_config --db .\my_project\.vscode\BROWSE.VC.DB --file src\config.c
python .\knowledge_base\src\cpp_meta_query.py calls parse_config --db .\my_project\.vscode --file src\config.c
python .\knowledge_base\src\cpp_meta_query.py calls parse_config --db .\my_project --file src\config.c
```

`--file`
: 源文件路径子串，用来在同名函数或同名符号候选中收窄范围，例如 `src\config.c`。

`--max-deps`
: 每类依赖最多输出多少项。`calls`、`params`、`report` 默认 `20`；`source` 默认 `200`；`subsource` 默认 `500`，用于容纳多个子函数的合并依赖。

`--max-candidates`
: 同名函数或同名符号候选最多保留多少项，默认 `12`。

`--max-snippet-lines`
: 结构体、宏、变量等依赖项的源码片段最多输出多少行，默认 `80`。

`--max-depth`
: `calls` 中控制向上追溯调用者的最大层数，默认 `5`；`subsource` 中控制向下递归解析子函数的最大层数，默认 `3`。

`--max-chains`
: 仅 `calls` 子命令使用，控制最多打印多少条调用链，默认 `200`。

`--max-callers-per-level`
: 仅 `calls` 子命令使用，控制每个函数名最多展开多少个直接调用者，默认 `80`。

`--max-functions`
: 仅 `subsource` 子命令使用，控制最多合并多少个函数源码，默认 `200`。

`--include-auxiliary-calls`
: 仅 `subsource` 子命令使用。默认不展开日志、trace、debug、统计/accounting、instrumentation 等辅助子函数；加上该选项后会把这些函数也纳入输出。

测试目录符号过滤
: `subsource` 和 `report` 中的下游子函数分析默认排除 `test`、`tests`、`testing`、`selftests`、`DT`、`ST` 等测试目录中的符号索引，避免测试代码里的同名定义造成输出重定义。

下游源码包去重
: `subsource` 最终生成的 `.c` 文件会按符号类别和名称去重，避免多个子函数共享同一个宏、typedef、枚举、变量、结构体或函数时重复输出定义。

`calls` 性能说明
: 调用链分析会按层批量搜索待查函数名。遇到 `can_send` 这类上游调用链较多的函数时，可用 `--max-depth`、`--max-chains`、`--max-callers-per-level` 控制输出规模。Windows 上优先确认 `rg` 可用，并把源码目录放在本地 SSD；源码在 OneDrive、网络盘或被杀毒实时扫描时，调用链抽取会明显变慢。

`--no-macros`
: 完整报告中的直接调用点和下游子函数分析可用该选项跳过大写宏风格调用。

`--max-nesting-depth`
: `source` 和 `subsource` 子命令使用，控制结构体/union/enum/typedef 递归解析层数，默认 `4` 层。

`--kind`
: 仅 `symbol` 子命令使用，限定非函数符号类别。可选 `macro`、`macro_define`、`typedef`、`enum`、`enumerator`、`variable`、`struct`、`union`。

## 子命令专用参数速查

`source`
: 导出 `.c` 分析包。常用参数是 `--output/-o`、`--max-nesting-depth`、`--max-deps`、`--max-snippet-lines`。

`subsource`
: 导出目标函数和下游子函数可编译 `.c` 分析包。常用参数是 `--output/-o`、`--max-depth`、`--max-functions`、`--max-nesting-depth`、`--max-deps`、`--include-auxiliary-calls`。

`calls`
: 打印上层调用链。常用参数是 `--max-depth`、`--max-chains`、`--max-callers-per-level`。

`params`
: 打印入参约束。通常需要结合 `--file` 定位目标函数。

`report`
: 打印四个功能的统一汇总报告。支持 `--format markdown|json` 和 `--no-macros`。

`symbol`
: 打印非函数符号源码片段。常用参数是 `--kind`、`--file`、`--max-candidates`、`--max-snippet-lines`。

## Linux 大型 C 工程验证案例

下面案例使用本地 `linux-7.0` 源码树验证工具在大型 C 工程上的行为。Linux
只是验证样例，不是工具的适用范围限制。运行这些案例时需要显式指定
`--repo .\linux-7.0`，或通过 `--db` 指向对应的 `BROWSE.VC.DB`。

### 案例 1：vfs_read 生成 .c 分析包

命令：

```powershell
python .\knowledge_base\src\cpp_meta_query.py source vfs_read --repo .\linux-7.0 --file fs\read_write.c --max-deps 4 --max-snippet-lines 8 --output .\test\fixtures\vfs_read_bundle_test.c
```

验证结果：

```text
Wrote source bundle: test\fixtures\vfs_read_bundle_test.c
```

生成文件包含：

```text
Target function:
  LINUX-7.0\FS\READ_WRITE.C:554-583

Constants/macros:
  EBADF
  EFAULT
  EINVAL
  FMODE_READ
  FMODE_CAN_READ
  MAX_RW_COUNT

Typedefs:
  loff_t
  size_t
  ssize_t
  fmode_t
  spinlock_t

Structures:
  struct file
  LINUX-7.0\INCLUDE\LINUX\FS.H:1259

Nested structures:
  struct file_operations
  struct address_space
  struct inode
  struct cred
  struct path
```

### 案例 1.1：vfs_read 生成子函数 .c 分析包

命令：

```powershell
python .\knowledge_base\src\cpp_meta_query.py subsource vfs_read --repo .\linux-7.0 --file fs\read_write.c --max-depth 1 --max-functions 20 --max-deps 20 --max-snippet-lines 20 --output .\test\fixtures\vfs_read_subfunctions_bundle_test.c
```

验证结果：

```text
Wrote subfunction source bundle: test\fixtures\vfs_read_subfunctions_bundle_test.c
```

生成文件包含：

```text
Root function:
  vfs_read

Compile support:
  minimal typedefs/macros/struct fields/stubs

Function bodies:
  rw_verify_area
  new_sync_read
  fsnotify_access
  vfs_read

Ordering:
  子函数源码位于 vfs_read 之前，按需合成的最小编译依赖位于所有函数源码之前。
  原始依赖以紧凑的符号引用摘要保留在分析区，不再内联大段注释源码。

Skipped auxiliary callees:
  add_rchar
  inc_syscr
```

### 案例 2：vfs_read 输出上层调用链路

命令：

```powershell
python .\knowledge_base\src\cpp_meta_query.py calls vfs_read --repo .\linux-7.0 --file fs\read_write.c --max-depth 3 --max-chains 20 --max-callers-per-level 20
```

### 案例 2.1：can_send 上层调用链路性能验证

命令：

```powershell
python .\knowledge_base\src\cpp_meta_query.py calls can_send --repo .\linux-7.0 --file net\can\af_can.c
```

验证结果摘要：

```text
Target: can_send (LINUX-7.0\NET\CAN\AF_CAN.C:202-300)
1. can_can_gw_rcv -> can_send
2. isotp_sendmsg -> can_send
3. raw_sendmsg -> can_send
4. bcm_tx_timeout_handler -> bcm_can_tx -> can_send
...
```

该案例用于验证上游链路较多时不会卡死。需要缩小输出时可执行：

```powershell
python .\knowledge_base\src\cpp_meta_query.py calls can_send --repo .\linux-7.0 --file net\can\af_can.c --max-depth 3 --max-chains 30 --max-callers-per-level 30
```

验证结果摘要：

```text
1. elf_fdpic_map_file -> elf_fdpic_map_file_constdisp_on_uclinux -> read_code -> vfs_read
2. load_flat_binary -> load_flat_file -> read_code -> vfs_read
3. SYSCALL_DEFINE3 -> ksys_read -> vfs_read
4. SYSCALL_DEFINE4 -> ksys_pread64 -> vfs_read
```

### 案例 3：vfs_read 输出入参约束

命令：

```powershell
python .\knowledge_base\src\cpp_meta_query.py params vfs_read --repo .\linux-7.0 --file fs\read_write.c
```

验证结果摘要：

```text
file:
  Type/format: file *file
  约束：指针参数，会通过 file->f_mode、file->f_op 解引用。

buf:
  Type/format: char __user *buf
  约束：用户态指针，通过 access_ok(buf, count) 校验访问范围。

count:
  Type/format: size_t count
  约束：参与 access_ok；当 count > MAX_RW_COUNT 时被截断为 MAX_RW_COUNT。

pos:
  Type/format: loff_t *pos
  约束：指针参数，会传递给 rw_verify_area/new_sync_read/read 回调。
```

### 案例 4：start_kernel 上层调用链路

命令：

```powershell
python .\knowledge_base\src\cpp_meta_query.py calls start_kernel --repo .\linux-7.0 --file init\main.c --max-depth 3
```

验证点：

```text
函数位置:
  LINUX-7.0\INIT\MAIN.C:1008-1219

调用链路说明:
  start_kernel 是内核启动入口之一，通常不会在 C 函数层面找到更上层普通函数调用者。
  如果没有上层调用者，脚本会输出 No upstream callers found.
```

### 案例 5：do_sys_openat2 结构体和入参约束

命令：

```powershell
python .\knowledge_base\src\cpp_meta_query.py source do_sys_openat2 --repo .\linux-7.0 --file fs\open.c --max-deps 8 --max-snippet-lines 12
python .\knowledge_base\src\cpp_meta_query.py calls do_sys_openat2 --repo .\linux-7.0 --file fs\open.c
python .\knowledge_base\src\cpp_meta_query.py params do_sys_openat2 --repo .\linux-7.0 --file fs\open.c
```

验证结果摘要：

```text
函数位置:
  LINUX-7.0\FS\OPEN.C:1357-1367

关键结构体:
  struct open_how
  LINUX-7.0\INCLUDE\UAPI\LINUX\OPENAT2.H:19

  struct open_flags
  LINUX-7.0\FS\INTERNAL.H:186

上层调用链路:
  SYSCALL_DEFINE3 -> do_sys_open -> do_sys_openat2
  SYSCALL_DEFINE4 -> do_sys_open -> do_sys_openat2
  COMPAT_SYSCALL_DEFINE3 -> do_sys_open -> do_sys_openat2
  SYSCALL_DEFINE4 -> do_sys_openat2

入参约束:
  dfd:
    int 类型；本函数体内没有显式比较约束。

  filename:
    const char __user *，只读用户态路径指针。

  how:
    struct open_how *，会通过 how->flags 解引用，也会传入 build_open_flags()。
```

### 案例 6：EINVAL 宏符号源码片段检索

命令：

```powershell
python .\knowledge_base\src\cpp_meta_query.py symbol EINVAL --repo .\linux-7.0 --kind macro --max-candidates 2 --max-snippet-lines 2
```

验证结果摘要：

````text
Symbol: EINVAL
Kind filter: macro
Candidates: 2 shown / 7 total

[1] macro_define EINVAL (...)
```c
#define ...
```
````

## 已知边界

当前数据库中 `symbols`、`symbol_refs`、`symbol_relations` 是空表，因此脚本无法直接从数据库获得精确调用图。现在采用的策略是：

1. 用 `code_items` 和 `files` 从 SQLite 中定位函数、参数、结构体、宏、变量等定义。
2. 读取函数源码，用启发式规则提取直接调用和入参约束。
3. 对函数指针调用，例如 `file->f_op->read()`，保留调用表达式但不强行解析到某个全局函数。
4. 对同名函数，建议使用 `--file` 指定路径子串，避免命中声明、平台实现或测试代码。

如果后续数据库能生成 `symbol_refs` 或 `symbol_relations`，脚本可以扩展为更精确的调用图分析。
