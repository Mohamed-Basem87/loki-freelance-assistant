import time
import uuid

from app.filters import keyword_filter
from app.logger import logger


async def process_message(event):

    start = time.perf_counter()

    job_uuid = str(uuid.uuid4())

    text = event.raw_text or ""

    result = keyword_filter(text)

    filter_time = round((time.perf_counter() - start) * 1000, 2)

    logger.create_job(
        job_uuid=job_uuid,
        job_id=str(event.id),
        source=event.chat.title if event.chat else "",
        title=text.splitlines()[0][:250] if text else "",
        company="",
        url="",
        score=result.get("score"),
        categories=result.get("categories", []),
        positive_matches=result.get("positive_matches", []),
        negative_matches=result.get("negative_matches", []),
        hard_reject=result["hard_reject"],
        notify_directly=result["notify_directly"],
        needs_gemini=result["needs_gemini"],
        decision_reason="Initial Filter",
        filter_time_ms=filter_time,
    )

    if result["hard_reject"]:

        logger.update_job(
            job_uuid,
            final_decision="Rejected",
            decision_reason="Hard Reject"
        )

        return

    if not result["matched"]:

        logger.update_job(
            job_uuid,
            final_decision="Rejected",
            decision_reason="No Matching Keywords"
        )

        return

    logger.update_job(
        job_uuid,
        final_decision="Matched",
        decision_reason="Passed Keyword Filter"
    )

    print("=" * 80)
    print(event.chat.title)
    print()
    print(text)
    print()
    print(result)
    print("=" * 80)

