from __future__ import annotations

import argparse
import sys

from .base import QueryOptions
from .call_chains import CallChainCommand
from .param_constraints import ParamConstraintsCommand
from .report import ReportCommand
from .source_bundle import SourceBundleCommand
from .subfunction_bundle import SubfunctionBundleCommand
from .symbol_lookup import SYMBOL_KIND_TO_DB_KIND, SymbolLookupCommand


DEFAULT_NESTING_DEPTH = 4


class HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    def _get_help_string(self, action: argparse.Action) -> str:
        if action.dest == "include_macros":
            return action.help or ""
        return super()._get_help_string(action)


def add_common(
    subparser: argparse.ArgumentParser,
    *,
    subject_name: str = "function",
    subject_help: str = "function name to query",
    max_deps_default: int = 20,
    max_snippet_lines_default: int = 80,
    show_max_deps: bool = True,
    show_no_macros: bool = False,
) -> None:
    subparser.add_argument(subject_name, help=subject_help)
    subparser.add_argument(
        "--repo",
        default=".",
        help=(
            "C/C++ source root. If --db points at a repo root or .vscode "
            "directory, the source root is inferred when possible."
        ),
    )
    subparser.add_argument(
        "--db",
        help=(
            "path to BROWSE.VC.DB, a directory containing BROWSE.VC.DB, "
            "or a repo root containing .vscode/BROWSE.VC.DB"
        ),
    )
    subparser.add_argument(
        "--file",
        help=(
            "substring used to disambiguate source file when multiple "
            "definitions share the same query name"
        ),
    )
    if show_max_deps:
        subparser.add_argument(
            "--max-deps",
            type=int,
            default=max_deps_default,
            help="maximum dependency snippets collected per category",
        )
    else:
        subparser.set_defaults(max_deps=max_deps_default)
    subparser.add_argument(
        "--max-candidates",
        type=int,
        default=12,
        help="maximum same-name function or symbol candidates retained",
    )
    subparser.add_argument(
        "--max-snippet-lines",
        type=int,
        default=max_snippet_lines_default,
        help="maximum source lines printed for each dependency snippet",
    )
    subparser.set_defaults(include_macros=True)
    subparser.add_argument(
        "--no-macros",
        dest="include_macros",
        action="store_false",
        default=argparse.SUPPRESS,
        help=(
            "exclude upper-case macro-like direct call sites from report details"
            if show_no_macros
            else argparse.SUPPRESS
        ),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query a VS Code C/C++ BROWSE.VC.DB by function or symbol name.\n"
            "查询 C/C++ 工程源码元数据库，导出源码片段、上层调用链和入参约束。"
        ),
        epilog="""common configuration:
  function/symbol          required query name, for example parse_config, MY_MACRO, or my_struct
  --repo PATH              C/C++ source root. Default: current directory (.)
  --db PATH                Metadata DB selector. Accepts:
                             1) path/to/BROWSE.VC.DB
                             2) path/to/.vscode
                             3) repo root containing .vscode/BROWSE.VC.DB
                           When --db is set, --repo is inferred when possible.
  --file TEXT              Source path substring used to choose one definition.
                           Example: --file src\\config.c
  --max-deps N             Dependency snippet limit. Default: 20; source default: 200;
                           subsource default: 500
  --max-candidates N       Maximum same-name function or symbol candidates. Default: 12
  --max-snippet-lines N    Maximum lines per dependency snippet. Default: 80;
                           subsource default: 120

command-specific configuration:
  source:
    --output, -o PATH      Output .c bundle. Default: <function>_source_bundle.c
    --max-nesting-depth N  Nested struct/union/enum/typedef recursion depth.
                           Default: 4 levels.
  subsource:
    --output, -o PATH      Output .c bundle. Default: <function>_subfunctions_bundle.c
    --max-depth N          Downstream child-function recursion depth. Default: 3
    --max-functions N      Maximum function bodies included. Default: 200
    --max-nesting-depth N  Nested struct/union/enum/typedef recursion depth.
                           Default: 4 levels.
    --include-auxiliary-calls
                           Include logging/trace/debug/stats helper callees.
  calls:
    --max-depth N          Upstream caller search depth. Default: 5
    --max-chains N         Maximum printed caller chains. Default: 200
    --max-callers-per-level N
                           Maximum direct callers explored per function. Default: 80
  report:
    --format markdown|json Output format. Default: markdown
    --no-macros            Hide upper-case macro-like direct call sites in report details.
  symbol:
    --kind KIND            Restrict non-function symbol kind. Choices:
                           macro, macro_define, typedef, enum, enumerator,
                           variable, struct, union

examples:
  python knowledge_base/src/cpp_meta_query.py --help
  python knowledge_base/src/cpp_meta_query.py source --help
  python knowledge_base/src/cpp_meta_query.py subsource parse_config --repo my_project --file src\\config.c --max-depth 2
  python knowledge_base/src/cpp_meta_query.py calls parse_config --db my_project\\.vscode\\BROWSE.VC.DB --file src\\config.c
  python knowledge_base/src/cpp_meta_query.py source parse_config --repo my_project --file src\\config.c --output parse_config_bundle.c
  python knowledge_base/src/cpp_meta_query.py calls parse_config --repo my_project --file src\\config.c --max-depth 5
  python knowledge_base/src/cpp_meta_query.py params parse_config --repo my_project --file src\\config.c
  python knowledge_base/src/cpp_meta_query.py report parse_config --repo my_project --file src\\config.c --no-macros
  python knowledge_base/src/cpp_meta_query.py symbol MY_MACRO --repo my_project --kind macro
  python knowledge_base/src/cpp_meta_query.py symbol my_struct --repo my_project --kind struct --file include\\types.h

notes:
  source and subsource recursively include nested struct/union/enum/typedef
  dependencies. The default nested parsing depth is 4 levels.
""",
        formatter_class=HelpFormatter,
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{source,subsource,calls,params,report,symbol}",
    )

    source_parser = subparsers.add_parser(
        "source",
        help="write function source and related snippets into a .c bundle",
        description=(
            "Export a .c analysis bundle containing constants/macros, typedefs, "
            "enums, global/static variables, recursively nested structs/unions, "
            "and the target function source."
        ),
        formatter_class=HelpFormatter,
    )
    add_common(source_parser, max_deps_default=200)
    source_parser.add_argument("--output", "-o", help="output .c file path")
    source_parser.add_argument(
        "--max-nesting-depth",
        type=int,
        default=DEFAULT_NESTING_DEPTH,
        help=(
            "nested struct/union/enum/typedef recursion depth for source "
            "bundles; default: %(default)s levels"
        ),
    )

    subsource_parser = subparsers.add_parser(
        "subsource",
        help="write target and downstream child functions into a .c bundle",
        description=(
            "Export a .c analysis bundle containing the target function, "
            "recursively resolved child functions, and all collected dependency snippets. "
            "Function bodies are ordered with callees before callers when possible. "
            "Symbols indexed from test/tests/testing/selftests/dt/st directories are excluded."
        ),
        formatter_class=HelpFormatter,
    )
    add_common(subsource_parser, max_deps_default=500, max_snippet_lines_default=120)
    subsource_parser.add_argument("--output", "-o", help="output .c file path")
    subsource_parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="maximum downstream child-function recursion depth",
    )
    subsource_parser.add_argument(
        "--max-functions",
        type=int,
        default=200,
        help="maximum number of function bodies included in the bundle",
    )
    subsource_parser.add_argument(
        "--max-nesting-depth",
        type=int,
        default=DEFAULT_NESTING_DEPTH,
        help=(
            "nested struct/union/enum/typedef recursion depth for dependency "
            "snippets; default: %(default)s levels"
        ),
    )
    subsource_parser.add_argument(
        "--include-auxiliary-calls",
        dest="include_auxiliary",
        action="store_true",
        help="include logging/trace/debug/stats/accounting helper callees",
    )
    subsource_parser.set_defaults(include_auxiliary=False)

    calls_parser = subparsers.add_parser(
        "calls",
        help="print upstream caller chains to the command line",
        description="Print upstream caller chains that reach the target function.",
        formatter_class=HelpFormatter,
    )
    add_common(calls_parser)
    calls_parser.add_argument("--max-depth", type=int, default=5)
    calls_parser.add_argument("--max-chains", type=int, default=200)
    calls_parser.add_argument("--max-callers-per-level", type=int, default=80)

    params_parser = subparsers.add_parser(
        "params",
        help="print inferred parameter constraints to the command line",
        description="Infer parameter type/format and likely constraints.",
        formatter_class=HelpFormatter,
    )
    add_common(params_parser)

    report_parser = subparsers.add_parser(
        "report",
        help="print a unified report for source, calls, params, and subsource",
        description=(
            "Print the unified report. It summarizes source dependencies with nested "
            "types, upstream caller chains, parameter constraints, and downstream "
            "subfunction analysis using the existing feature defaults."
        ),
        formatter_class=HelpFormatter,
    )
    add_common(report_parser, show_no_macros=True)
    report_parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="report output format",
    )
    symbol_parser = subparsers.add_parser(
        "symbol",
        help="print source snippets for non-function symbols",
        description=(
            "Search non-function symbols such as macros, typedefs, enums, variables, "
            "structs, and unions, then print matching source snippets to stdout."
        ),
        formatter_class=HelpFormatter,
    )
    add_common(
        symbol_parser,
        subject_name="symbol",
        subject_help="non-function symbol name to query",
        max_deps_default=20,
        show_max_deps=False,
    )
    symbol_parser.add_argument(
        "--kind",
        choices=tuple(sorted(SYMBOL_KIND_TO_DB_KIND)),
        help="restrict lookup to one non-function symbol kind",
    )
    return parser.parse_args(argv)


def options_from_args(args: argparse.Namespace) -> QueryOptions:
    return QueryOptions(
        repo=args.repo,
        db=args.db,
        file_filter=args.file,
        include_macros=args.include_macros,
        max_deps=args.max_deps,
        max_candidates=args.max_candidates,
        max_snippet_lines=args.max_snippet_lines,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    options = options_from_args(args)
    if args.command == "source":
        output = SourceBundleCommand(options).export(
            args.function,
            args.output,
            max_nesting_depth=args.max_nesting_depth,
        )
        print(f"Wrote source bundle: {output}")
        return 0
    if args.command == "subsource":
        output = SubfunctionBundleCommand(options).export(
            args.function,
            args.output,
            include_auxiliary=args.include_auxiliary,
            max_depth=args.max_depth,
            max_functions=args.max_functions,
            max_nesting_depth=args.max_nesting_depth,
        )
        print(f"Wrote subfunction source bundle: {output}")
        return 0
    if args.command == "calls":
        CallChainCommand(options).print(
            args.function,
            max_depth=args.max_depth,
            max_chains=args.max_chains,
            max_callers_per_level=args.max_callers_per_level,
        )
        return 0
    if args.command == "params":
        ParamConstraintsCommand(options).print(args.function)
        return 0
    if args.command == "symbol":
        SymbolLookupCommand(options).print(args.symbol, kind=args.kind)
        return 0

    ReportCommand(options).print(args.function, output_format=args.format)
    return 0

