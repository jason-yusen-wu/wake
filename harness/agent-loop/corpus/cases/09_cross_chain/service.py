def get_user(user_id):
    """Return a user record, or None if the user does not exist."""
    row = query_user(user_id)
    return row
