"""Job-source registry: a pure wiring/registration table.

The composition root constructs every source's collaborators and registers
a per-source factory (a closure over those collaborators) via
``register(source_id, factory)``. ``build(source_id)`` only invokes a
factory that was already registered -- it performs no dependency
discovery, no importlib path loading based on config strings, and no
introspection of factory signatures. This registry never reaches into
app.config / app.state_store / app.parser / app.freehub to fabricate
dependencies; there is no hidden service locator here.

The only config the registry touches is ``active_ids``, which reports the
*configured* set of job-source adapter ids (selection, not dependency
resolution).
"""

_FACTORIES = {}


def register(source_id, factory):
    _FACTORIES[source_id.strip().lower()] = factory


def build(source_id, **kwargs):
    key = source_id.strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        raise KeyError(
            f"Job source {source_id!r} has no registered factory. "
            "Source factories are registered by the composition root "
            "(app.composition.compose) after constructing the source's "
            "collaborators."
        )
    return factory(**kwargs)


def active_ids(configured=None):
    if configured is not None:
        return tuple(x.strip().lower() for x in configured if x.strip())
    from app.runtime_config import JOB_SOURCES

    return tuple(cfg.id.lower() for cfg in JOB_SOURCES if cfg.enabled)