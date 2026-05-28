from typing import Optional

def get_username(user_id: int) -> Optional[str]:
    return None

def render_user(user_id: int) -> str:
    name = get_username(user_id)
    header = name.upper()
    footer = name.center(40, "-")
    return header + "\n" + footer
