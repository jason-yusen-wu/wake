from typing import Optional

def fetch_setting(key: str) -> Optional[str]:
    return None

def apply_setting(key: str) -> None:
    value = fetch_setting(key)
    assert value is not None, f"Required setting '{key}' is missing"
    value.upper()
    value.strip()
