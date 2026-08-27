from app.logger import logger as excel_logger


async def log_guard_decision(
    *,
    job_uuid,
    source="",
    title="",
    original_decision="",
    guard_decision="",
    provider="Groq",
    model="",
    response_time_ms=None,
    error="",
    guard_category="",
):
    """
    Persist one guard decision through the shared DB logger.

    The row is inserted into the `notification_guard` table on the
    logger's single dedicated worker thread (see DBLogger.run).

    `guard_category` is written in the same insert as `guard_decision`
    so the two are always durably consistent together -- see
    NOTIFICATION_GUARD_HEADERS / get_latest_guard_decision_with_category
    in app.logger for why this atomicity matters.
    """

    await excel_logger.run(
        excel_logger.log_notification_guard,
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
