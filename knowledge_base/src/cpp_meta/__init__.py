"""C/C++ metadata query package."""

from .source_bundle import analyze_function, export_source_bundle
from .call_chains import print_function_call_sequence
from .param_constraints import print_function_param_constraints
from .subfunction_bundle import export_subfunction_source_bundle
from .symbol_lookup import lookup_symbol_source, print_symbol_source

__all__ = [
    "analyze_function",
    "export_source_bundle",
    "print_function_call_sequence",
    "print_function_param_constraints",
    "export_subfunction_source_bundle",
    "lookup_symbol_source",
    "print_symbol_source",
]

