from typing import Optional

def fetch_setting(key: str) -> Optional[str]:
    return None

def apply_setting(key: str) -> None:
    value = fetch_setting(key)
    value.upper()
    value.strip()
