"""Parser composition facade. The default registry remains unchanged."""
from app.adapters.parsers.registry import default_parser_registry

_parser_registry = None

def get_parser_registry():
    global _parser_registry
    if _parser_registry is None: _parser_registry = default_parser_registry()
    return _parser_registry

def set_parser_registry(registry):
    global _parser_registry; _parser_registry = registry

def parse_job(source, text): return get_parser_registry().parse(source, text)
