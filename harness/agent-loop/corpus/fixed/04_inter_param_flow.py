from typing import Optional

def wrap_value(val: Optional[str]) -> Optional[str]:
    return val

def process_input(text: Optional[str]) -> str:
    result = wrap_value(text)
    if result is None:
        return ""
    return result.strip()
