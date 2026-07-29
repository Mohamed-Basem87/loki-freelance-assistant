from app.llm.gemini import evaluate_job as gemini_evaluate
from app.llm.groq import evaluate_job as groq_evaluate


def evaluate_job(text: str, filter_result: dict):

    try:
        return gemini_evaluate(text, filter_result)

    except Exception as gemini_error:

        print(f"Gemini failed: {gemini_error}")
        print("Falling back to Groq...")

        try:
            return groq_evaluate(text, filter_result)

        except Exception as groq_error:

            print(f"Groq failed: {groq_error}")

            raise RuntimeError(
                 f"All LLM providers failed. "
                 f"Gemini: {gemini_error} | "
                 f"Groq: {groq_error}"
            ) from groq_error