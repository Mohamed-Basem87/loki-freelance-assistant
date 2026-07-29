from app.gemini import evaluate_job as gemini_evaluate


def evaluate_job(text: str, filter_result: dict):
    return gemini_evaluate(text, filter_result)