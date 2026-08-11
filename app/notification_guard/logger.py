from datetime import datetime

from app.logger import logger as excel_logger


NOTIFICATION_GUARD_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Source",
    "Title",
    "Original Decision",
    "Guard Decision",
    "Provider",
    "Model",
    "Response Time (ms)",
    "Error",
]


def _append_guard_row(
    job_uuid,
    source,
    title,
    original_decision,
    guard_decision,
    provider,
    model,
    response_time_ms,
    error,
):
    """
    This function MUST only be called through excel_logger.run().

    It therefore executes on the existing ExcelLogger's single
    dedicated worker thread and shares its existing Workbook,
    thread-safety guarantees, and atomic save mechanism.
    """

    workbook = excel_logger.workbook

    if "NotificationGuard" not in workbook.sheetnames:
        sheet = workbook.create_sheet("NotificationGuard")
        sheet.append(NOTIFICATION_GUARD_HEADERS)
    else:
        sheet = workbook["NotificationGuard"]

    sheet.append([
        datetime.now().isoformat(),
        job_uuid,
        source,
        title,
        original_decision,
        guard_decision,
        provider,
        model,
        response_time_ms,
        error,
    ])

    excel_logger.save()


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
):
    """
    Persist one guard decision through the existing ExcelLogger.

    No separate Workbook, executor, or save implementation is created.
    """

    await excel_logger.run(
        _append_guard_row,
        job_uuid,
        source,
        title,
        original_decision,
        guard_decision,
        provider,
        model,
        response_time_ms,
        error,
    )
