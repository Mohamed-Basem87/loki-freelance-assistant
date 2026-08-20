from app.llm.gemini import (
    evaluate_category_arbitration as gemini_arbitrate,
    evaluate_job as gemini_evaluate,
)
from app.llm.groq import (
    evaluate_category_arbitration as groq_arbitrate,
    evaluate_job as groq_evaluate,
)


def evaluate_job(text: str, filter_result: dict, system_prompt: str = None):
    if system_prompt is None:
        from app.categories.data_analysis.llm_prompt import SYSTEM_PROMPT
        system_prompt = SYSTEM_PROMPT
    try:
        return gemini_evaluate(text, filter_result, system_prompt)
    except Exception as gemini_error:
        print(f"Gemini failed: {gemini_error}")
        print("Falling back to Groq...")
        try:
            return groq_evaluate(text, filter_result, system_prompt)
        except Exception as groq_error:
            print(f"Groq failed: {groq_error}")
            raise RuntimeError(
                f"All LLM providers failed. "
                f"Gemini: {gemini_error} | Groq: {groq_error}"
            ) from groq_error


def arbitrate_category(text: str, candidates: list[dict], system_prompt: str):
    """Make exactly one provider arbitration request for all candidates."""
    try:
        return gemini_arbitrate(text, candidates, system_prompt)
    except Exception as gemini_error:
        print(f"Gemini arbitration failed: {gemini_error}")
        print("Falling back to Groq arbitration...")
        try:
            return groq_arbitrate(text, candidates, system_prompt)
        except Exception as groq_error:
            print(f"Groq arbitration failed: {groq_error}")
            raise RuntimeError(
                f"All category-arbitration providers failed. "
                f"Gemini: {gemini_error} | Groq: {groq_error}"
            ) from groq_error
