from typing import Optional

def clean_input(text: Optional[str]) -> str:
    if text is None:
        return ""
    return text.strip()
