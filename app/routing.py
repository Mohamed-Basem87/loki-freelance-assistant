"""User routing for final, single-category jobs.

A user may subscribe to many categories, but each job carries exactly
one final category. Routing therefore creates at most one pending
notification per (job, user) pair.
"""


async def queue_for_category(job_uuid, category_id, source="", repository=None):
    if not category_id:
        return 0
    if repository is None:
        # Legacy fallback for callers that have not been wired by the
        # composition root (tests, external callers). Production passes
        # the repository through the guard wrapper's construction
        # boundary and never reaches into the service locator here.
        from app.dependencies import logger as _logger
        repository = _logger
    return await repository.queue_user_notifications(job_uuid, category_id, source, save=True)
