async def log_guard_decision(
    *,
    job_uuid,
    source="",
    title="",
    original_decision="",
    guard_decision="",
    provider="",
    model="",
    response_time_ms=None,
    error="",
    guard_category="",
    repository=None,
):
    """
    Persist one guard decision through the shared DB logger.

    The row is inserted into the `notification_guard` table on the
    logger's single dedicated worker thread (see DBLogger.run).

    `guard_category` is written in the same insert as `guard_decision`
    so the two are always durably consistent together -- see
    NOTIFICATION_GUARD_HEADERS / get_latest_guard_decision_with_category
    in app.logger for why this atomicity matters.

    ``repository`` is injected by the notification guard (which owns its
    configured repository). When omitted -- e.g. tests or standalone
    callers that have not been wired by the composition root -- the
    legacy service-locator binding is used as a fallback.
    """
    repository = repository or _default_repository()

    await repository.log_notification_guard(
        job_uuid,
        source,
        title,
        original_decision,
        guard_decision,
        provider,
        model,
        response_time_ms,
        error,
        guard_category,
    )


def _default_repository():
    # Legacy fallback for callers that have not been wired by the
    # composition root (the guard path always injects its own repository).
    from app.dependencies import logger as _logger
    return _logger
