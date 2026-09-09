from dataclasses import dataclass
from collections.abc import Callable
from . import common
from . import mostaql, nafezly, generic


@dataclass(frozen=True)
class ParserAdapter:
    id: str
    aliases: tuple[str, ...]
    parse: Callable[[str, str], dict]

    def matches(self, source: str) -> bool:
        import re
        value = (source or "").strip().casefold()
        if value == self.id.casefold():
            return True
        return any(
            re.search(rf"(?<!\w){re.escape(alias.casefold())}(?!\w)", value)
            for alias in self.aliases
        )


class ParserRegistry:
    def __init__(self, adapters=()):
        self._adapters = tuple(adapters)

    def register(self, adapter: ParserAdapter):
        self._adapters = (*self._adapters, adapter)

    def resolve(self, source: str) -> ParserAdapter:
        for adapter in self._adapters:
            if adapter.matches(source):
                return adapter
        return ParserAdapter("generic", (), generic.parse)

    def parse(self, source: str, text: str) -> dict:
        return self.resolve(source).parse(source, text)


def default_parser_registry() -> ParserRegistry:
    return ParserRegistry((
        ParserAdapter("nafezly", ("nafezly", "نفذلي"), nafezly.parse),
        ParserAdapter("mostaql", ("mostaql", "مستقل"), mostaql.parse),
    ))
