# cpp_meta_query API 实现细节

本文档说明 `cpp_meta_query.py` 各 API 在解析函数、子函数和依赖符号时的内部选择逻辑。
它和 `cpp_meta_query.md` 分工不同：后者面向命令用法和验证案例，本文面向实现细节、
边界行为和歧义处理。

## 基本原则

脚本读取 VS Code C/C++ 插件生成的 `BROWSE.VC.DB`。该数据库主要提供源码符号索引，
不等同于完整编译器语义分析结果。因此当前实现采用“数据库候选 + 源码启发式评分”的方式：

- 函数分析和依赖分析中，当同名函数或同名依赖符号在数据库中出现多次时，
  默认选择评分最高的一个候选。
- `symbol` 是检索接口，不做唯一候选裁剪；同名非函数符号会按顺序打印多个候选。
- `source`、`params`、`report` 的目标函数可以通过 `--file` 缩小候选范围。
- `subsource` 会在最终 `.c` 文件中再次去重，避免多个子函数共享依赖时造成重复定义。
- 上游调用链 `calls` 是名称级源码搜索，不是基于数据库 symbol id 的精确反向引用。

## 目标函数选择

适用接口：

- CLI：`source`、`subsource`、`calls`、`params`、`report`
- Python API：`export_source_bundle`、`export_subfunction_source_bundle`、
  `print_function_call_sequence`、`print_function_param_constraints`、`analyze_report`

实现入口：

- `knowledge_base/src/cpp_meta/db.py::find_functions`
- `knowledge_base/src/cpp_meta/base.py::CppMetaCommand.select_function`

处理流程：

1. 在 `code_items` 中查找 `kind = 27` 且 `name = 输入函数名` 的函数符号。
2. 如果用户传入 `--file` 或 Python API 的 `file_filter`，则额外要求文件路径包含该子串。
3. 候选函数按源码跨度排序：

```text
(end_line - start_line) 越大越靠前
start_line 越小越靠前
```

4. 当前 API 选择排序后的第一个候选作为目标函数。
5. 其余同名候选保存到 `ambiguous_candidates`，数量受 `--max-candidates` 控制。

影响：

- 如果多个文件中存在同名函数，默认会选源码跨度最大的那个。
- 如果要稳定选择某个源文件里的定义，应显式传入 `--file`。
- `ambiguous_candidates` 只用于提示和报告，不会自动展开多个目标函数。

## 下游子函数选择

适用接口：

- CLI：`subsource`、`report`
- Python API：`export_subfunction_source_bundle`、`analyze_report` 中的下游摘要

实现入口：

- `knowledge_base/src/cpp_meta/calls.py::discover_calls`
- `knowledge_base/src/cpp_meta/db.py::resolve_function`
- `knowledge_base/src/cpp_meta/calls.py::collect_downstream_functions`

处理流程：

1. 先从函数源码中用正则识别直接调用表达式，例如 `foo()`。
2. 控制流关键字如 `if`、`for`、`while` 会被过滤。
3. 成员调用和函数指针风格调用，例如 `obj->run()`、`ops.read()`，会保留在源码中，
   但不会强行解析到某个全局函数定义。
4. 对普通直接调用名，查询数据库中所有同名函数候选。
5. `resolve_function` 对候选打分并选择最高分的定义。

当前打分逻辑：

```text
同调用者文件: +1000
调用者不在 tools 目录时，候选也不在 tools 目录: +300
调用者不在 tools 目录时，候选在 tools 目录: -300
候选 attributes 含定义标记: +100
候选源码跨度为 0: -100
候选源码跨度越大，排序时越占优
```

`subsource` 还会额外过滤：

- 测试目录符号，例如 `test`、`tests`、`testing`、`selftests`、`DT`、`ST`。
- 默认跳过日志、trace、debug、统计/accounting、instrumentation 等辅助调用。
- `span <= 0` 的候选不会作为可展开子函数。

影响：

- 如果同名子函数同时存在于多个文件，优先选择调用者同文件的定义。
- 对大量 `static helper()` 这类同名局部函数，同文件优先通常能避免选错。
- 如果不同文件的同名函数都可能合法，当前实现不会输出所有变体，只输出评分最高者。

## 依赖符号选择

适用接口：

- CLI：`source`、`subsource`、`report`
- Python API：`export_source_bundle`、`export_subfunction_source_bundle`、`analyze_report`

依赖符号包括：

- 宏和常量
- `typedef`
- 枚举和枚举值
- 全局变量
- 静态变量
- 结构体、union 和嵌套类型

实现入口：

- `knowledge_base/src/cpp_meta/dependencies.py::lookup_items_for_tokens`
- `knowledge_base/src/cpp_meta/dependencies.py::score_item`
- `knowledge_base/src/cpp_meta/dependencies.py::classify_dependencies`

处理流程：

1. 从目标函数源码、函数签名和参数类型中提取 token。
2. 在数据库中查找这些 token 对应的感兴趣符号。
3. 如果启用测试目录过滤，先排除测试目录下的候选符号。
4. 按 `(name, kind)` 分组。
5. 每个 `(name, kind)` 分组只保留评分最高的一个候选。

当前依赖符号打分逻辑：

```text
候选和当前函数同文件:
  结构体 / union / enum / typedef: +100
  其他符号: +1000
候选路径包含 include: +80
候选 parent_id == 1024: +40
候选 attributes 含定义标记: +500
候选 attributes 不含定义标记: -30
候选源码跨度: 最多 +200
函数式宏被源码以 call 形式使用: -40
```

随后 `classify_dependencies` 会把候选归类到输出分组中。函数参数名、局部变量名、
成员访问名和直接调用名通常会作为局部上下文过滤，避免把局部变量误当成全局依赖。

影响：

- 数据库中存在多个同名宏、同名结构体或同名 typedef 时，每类只选择一个最佳候选。
- 同文件的变量、宏等普通符号会明显优先于其他文件。
- 类型符号同文件也会优先，但权重低于变量和宏，因为类型更常来自公共头文件。

## symbol 非函数符号检索

适用接口：

- CLI：`symbol`
- Python API：`lookup_symbol_source`、`print_symbol_source`

实现入口：

- `knowledge_base/src/cpp_meta/db.py::find_symbols`
- `knowledge_base/src/cpp_meta/symbol_lookup.py::SymbolLookupCommand.build_report`

处理流程：

1. 在 `code_items` 中查找 `name = 输入符号名` 的非函数符号。
2. 默认搜索宏、typedef、枚举、枚举值、变量、结构体和 union。
3. 如果用户传入 `--kind` 或 Python API 的 `kind`，则只搜索对应类别。
4. 如果用户传入 `--file` 或 Python API 的 `file_filter`，则要求文件路径包含该子串。
5. 按候选顺序输出多个源码片段，数量由 `--max-candidates` 控制。
6. 每个源码片段最多输出 `--max-snippet-lines` 行。

当前排序逻辑：

```text
macro_define
typedef
enum
enumerator
variable
struct
union
其他
```

同一类别内继续按源码跨度、文件路径和起始行排序。

影响：

- `symbol` 和函数分析不同，它是显式检索接口，不会只选择一个最佳符号。
- 如果同名宏或同名结构体出现在多个头文件中，终端会打印多个候选。
- 可用 `--kind` 和 `--file` 收窄候选范围，便于定位目标源码片段。
- `symbol` 不检索函数定义；函数源码请使用 `source` 或 `subsource`。

## 嵌套类型解析

适用接口：

- CLI：`source`、`subsource`、`report`
- Python API：`export_source_bundle`、`export_subfunction_source_bundle`、`analyze_report`

实现入口：

- `knowledge_base/src/cpp_meta/dependencies.py::expand_nested_type_dependencies`
- `knowledge_base/src/cpp_meta/dependencies.py::lookup_nested_type_items`

处理流程：

1. 从已选中的结构体、union、enum、typedef 片段中继续提取类型 token。
2. 对 `struct foo`、`union bar`、`enum baz` 这类 tag 类型，查找结构体、union、enum。
3. 对 typedef 类型名，查找 typedef。
4. 继续使用依赖符号的候选选择逻辑。
5. 用数据库 `id` 去重，避免同一个符号反复入队。
6. 递归层数由 `--max-nesting-depth` 控制，默认 4 层。

影响：

- 嵌套类型不是简单字符串展开，而是继续回到数据库里按候选评分选择。
- 如果同名嵌套类型有多个数据库候选，仍然只保留评分最高的一个。

## subsource 合并和最终去重

适用接口：

- CLI：`subsource`
- Python API：`export_subfunction_source_bundle`

实现入口：

- `knowledge_base/src/cpp_meta/subfunction_bundle.py::SubfunctionBundleCommand.build_report`
- `knowledge_base/src/cpp_meta/dependencies.py::merge_dependency_groups`
- `knowledge_base/src/cpp_meta/renderer.py::render_subfunction_c_bundle`

处理流程：

1. 从目标函数开始向下递归解析可定位的普通直接调用。
2. 对每个已收集函数分别采集依赖。
3. 合并依赖时先按数据库 `id` 去重。
4. 渲染最终 `.c` 文件时，再按“符号类别 + 名称”去重。
5. 函数源码按函数名去重。

最终 `.c` 文件去重范围：

```text
macro_define + name
typedef + name
enum / enumerator + name
variable + name
struct / union + name
function name
```

影响：

- 多个子函数共同依赖同一个宏、结构体、typedef、枚举或变量时，最终只输出一份定义。
- 如果两个不同文件中存在同名 static 函数，最终 `.c` 也只输出一个同名函数体，
  以避免单个 `.c` 分析包中出现重复函数定义。
- 这种策略优先保证知识库分析包可读、少重定义；代价是不会保留同名符号的所有变体。

## 上游调用链的歧义

适用接口：

- CLI：`calls`、`report`
- Python API：`print_function_call_sequence`、`analyze_report`

实现入口：

- `knowledge_base/src/cpp_meta/calls.py::find_direct_callers_multi`
- `knowledge_base/src/cpp_meta/calls.py::build_upstream_call_chains`

处理流程：

1. 使用 `rg` 或文件遍历按函数名搜索 `callee_name(`。
2. 根据命中行在数据库中查找所在的 enclosing function。
3. 确认该行在函数源码中确实包含目标调用名。
4. 按层向上扩展调用者，输出 `upper -> middle -> target` 链路。

影响：

- `calls` 是名称级调用链，不是基于目标函数数据库 `id` 的精确引用关系。
- 如果多个同名函数存在，调用链可能表示“调用了这个名字”，但无法完全证明调用的是哪一个定义。
- 对同名函数较多的工程，应结合 `--file`、`--max-depth`、`--max-chains`、
  `--max-callers-per-level` 控制和人工复核。

## params 的歧义边界

适用接口：

- CLI：`params`、`report`
- Python API：`print_function_param_constraints`、`analyze_report`

`params` 首先使用目标函数选择逻辑确定一个函数定义，然后只基于该函数的源码、参数符号和证据行
推断入参约束。它不会对同名函数的所有候选分别输出约束。

影响：

- 如果目标函数选择错了，入参约束自然也会对应错误的函数定义。
- 对同名函数，建议使用 `--file` 明确目标源文件。

## 使用建议

遇到同名函数或同名符号较多的工程，建议：

1. 对目标函数使用 `--file` 明确源文件路径子串。
2. 对多个数据库候选保持关注，查看报告中的 `ambiguous_candidates`。
3. 对 `subsource` 生成的 `.c` 文件，优先把它视为知识库和 harness 生成上下文，
   不要假设它是可直接编译的完整翻译单元。
4. 如果发现某个同名符号选择不符合预期，优先收窄 `--file`、`--db` 或源码根目录。
5. 对上游调用链结果，重点关注调用路径和证据位置，必要时回到源码人工确认具体同名目标。
