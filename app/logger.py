from pathlib import Path
from datetime import datetime

from openpyxl import Workbook, load_workbook


LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "freelance_bot_logs.xlsx"


JOB_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Job ID",
    "Source",
    "Title",
    "Company",
    "URL",
    "Score",
    "Categories",
    "Positive Matches",
    "Negative Matches",
    "Hard Reject",
    "Notify Directly",
    "Needs Gemini",
    "Gemini Decision",
    "Notification Status",
    "Final Decision",
    "Decision Reason",
    "Filter Time (ms)",
]


GEMINI_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Score Before",
    "Prompt Tokens",
    "Completion Tokens",
    "Response Time (ms)",
    "Decision",
    "Confidence",
]


NOTIFICATION_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Platform",
    "Status",
]


ERROR_HEADERS = [
    "Timestamp",
    "Module",
    "Error",
]


COLUMN_MAP = {
    "timestamp": 1,
    "job_uuid": 2,
    "job_id": 3,
    "source": 4,
    "title": 5,
    "company": 6,
    "url": 7,
    "score": 8,
    "categories": 9,
    "positive_matches": 10,
    "negative_matches": 11,
    "hard_reject": 12,
    "notify_directly": 13,
    "needs_gemini": 14,
    "gemini_decision": 15,
    "notification_status": 16,
    "final_decision": 17,
    "decision_reason": 18,
    "filter_time_ms": 19,
}


class ExcelLogger:

    def __init__(self):
        self.path = LOG_FILE
        self.workbook = None
        self._row_index: dict[str, int] = {}

    def initialize(self):

        self.path.parent.mkdir(parents=True, exist_ok=True)

        if not self.path.exists():

            wb = Workbook()

            wb.remove(wb.active)

            jobs = wb.create_sheet("Jobs")
            jobs.append(JOB_HEADERS)

            gemini = wb.create_sheet("Gemini")
            gemini.append(GEMINI_HEADERS)

            notifications = wb.create_sheet("Notifications")
            notifications.append(NOTIFICATION_HEADERS)

            errors = wb.create_sheet("Errors")
            errors.append(ERROR_HEADERS)

            wb.save(self.path)

        self.workbook = load_workbook(self.path)

        self._build_row_index()

    def _build_row_index(self):

        self._row_index.clear()

        ws = self.workbook["Jobs"]

        for row in range(2, ws.max_row + 1):

            job_uuid = ws.cell(
                row=row,
                column=COLUMN_MAP["job_uuid"],
            ).value

            if job_uuid:
                self._row_index[job_uuid] = row

    def save(self):
        self.workbook.save(self.path)

    def close(self):
        self.save()
        self.workbook.close()

    def create_job(
        self,
        job_uuid,
        job_id="",
        source="",
        title="",
        company="",
        url="",
        score=None,
        categories=None,
        positive_matches=None,
        negative_matches=None,
        hard_reject=False,
        notify_directly=False,
        needs_gemini=False,
        decision_reason="",
        filter_time_ms=None,
    ):

        ws = self.workbook["Jobs"]

        ws.append([
            datetime.now().isoformat(),
            job_uuid,
            job_id,
            source,
            title,
            company,
            url,
            score,
            ", ".join(categories or []),
            ", ".join(positive_matches or []),
            ", ".join(negative_matches or []),
            hard_reject,
            notify_directly,
            needs_gemini,
            "",
            "",
            "",
            decision_reason,
            filter_time_ms,
        ])

        self._row_index[job_uuid] = ws.max_row

        self.save()

    def update_job(self, job_uuid, **fields):

        row = self._row_index.get(job_uuid)

        if row is None:
            return False

        ws = self.workbook["Jobs"]

        for key, value in fields.items():

            if key not in COLUMN_MAP:
                continue

            if isinstance(value, list):
                value = ", ".join(value)

            ws.cell(
                row=row,
                column=COLUMN_MAP[key],
            ).value = value

        self.save()

        return True

    def log_gemini(
        self,
        job_uuid,
        score_before,
        prompt_tokens,
        completion_tokens,
        response_time_ms,
        decision,
        confidence,
    ):

        ws = self.workbook["Gemini"]

        ws.append([
            datetime.now().isoformat(),
            job_uuid,
            score_before,
            prompt_tokens,
            completion_tokens,
            response_time_ms,
            decision,
            confidence,
        ])

        self.save()

    def log_notification(
        self,
        job_uuid,
        platform,
        status,
    ):

        ws = self.workbook["Notifications"]

        ws.append([
            datetime.now().isoformat(),
            job_uuid,
            platform,
            status,
        ])

        self.save()

    def log_error(
        self,
        module,
        error,
    ):

        ws = self.workbook["Errors"]

        ws.append([
            datetime.now().isoformat(),
            module,
            str(error),
        ])

        self.save()


logger = ExcelLogger()


def initialize_workbook():
    logger.initialize()
