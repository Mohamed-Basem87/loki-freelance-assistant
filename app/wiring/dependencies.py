"""Late-bound dependency slots used by the composition root.

Compatibility binding remains for legacy module-level imports, but persistence
and state proxies expose explicit allow-lists so arbitrary infrastructure APIs
cannot leak into application code.

New production code must use the canonical interfaces injected by the
composition root (see app.wiring.composition). These proxies exist only as a
thin compatibility delegation layer for modules that have not yet been
migrated to constructor injection. They contain NO independent business
logic.
"""

class DependencyProxy:
    """Thin delegation proxy. After configure() runs, attribute access
    forwards to the real bound instance. No independent logic."""
    def __init__(self, name, allowed=None):
        self.name, self._value = name, None
        self._allowed = frozenset(allowed or ())
    def bind(self, value): self._value = value
    def _get(self):
        if self._value is None:
            raise RuntimeError(
                f"DependencyProxy '{self.name}' accessed before "
                "composition root configure() was called. "
                "Ensure app.wiring.composition.compose() runs first."
            )
        return self._value
    def __getattr__(self, name):
        if self._allowed and name not in self._allowed:
            raise AttributeError(f"Dependency '{self.name}' does not expose '{name}'")
        return getattr(self._get(), name)
    def __call__(self, *a, **kw): return self._get()(*a, **kw)

from app.ports import JobRepository, StateStore, DedupStore
# Legacy persistence handle exposed under the "logger" name: before the
# JobRepository port existed, DB logging WAS the repository surface, and
# several not-yet-constructor-injected modules (job_processor,
# message_processor) still call arbitrary repository methods through
# this proxy. The allow-list therefore deliberately carries the full
# JobRepository method set -- it cannot shrink to the log_* methods without
# migrating those modules to constructor injection first. Compatibility shim
# only; new production code must take the repository via composition.
logger = DependencyProxy("logger", allowed=set(JobRepository._METHODS))
state = DependencyProxy("state", allowed={name for cls in (StateStore, DedupStore) for name in cls.__dict__ if not name.startswith("_")})
dedup = DependencyProxy("dedup", allowed={name for name in DedupStore.__dict__ if not name.startswith("_")})
parser = DependencyProxy("parser", allowed={"parse", "resolve", "register"})
resolver = DependencyProxy("resolver", allowed=None)
# guard_allow: NotificationGuardIntegration.allow -- the guard's
# notify/do-not-notify decision, called once per job right before
# stream_publisher.publish(). stream_publisher: publishes a
# guard-approved job for the separate notification/delivery service to
# consume (see app.ports.JobStreamPublisher).
guard_allow = DependencyProxy("guard_allow", allowed=None)
stream_publisher = DependencyProxy("stream_publisher", allowed={"publish"})
url_shortener = DependencyProxy("url_shortener", allowed={"shorten"})

def configure(*, persistence=None, state_store=None, dedup_store=None, parser_registry=None, notification_resolver=None, guard_allow_fn=None, stream_publisher_service=None, url_shortener_service=None):
    """Bind real instances into the proxy slots. Called once by the
    composition root. All proxies must be bound before any worker
    starts."""
    if persistence is not None: logger.bind(persistence)
    if state_store is not None: state.bind(state_store)
    if dedup_store is not None: dedup.bind(dedup_store)
    if parser_registry is not None: parser.bind(parser_registry)
    if notification_resolver is not None: resolver.bind(notification_resolver)
    if guard_allow_fn is not None: guard_allow.bind(guard_allow_fn)
    if stream_publisher_service is not None: stream_publisher.bind(stream_publisher_service)
    if url_shortener_service is not None: url_shortener.bind(url_shortener_service)
