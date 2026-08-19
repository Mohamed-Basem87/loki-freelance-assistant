from app.categories.base import CategoryProfile
from app.categories.data_analysis import guard_prompt, keywords, llm_prompt


PROFILE = CategoryProfile(
    id="data_analysis",
    name="Data Analysis",
    keywords=keywords,
    llm_prompt=llm_prompt,
    guard_prompt=guard_prompt,
)
