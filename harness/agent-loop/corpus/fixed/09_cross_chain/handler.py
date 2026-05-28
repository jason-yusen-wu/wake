def handle_get_user(user_id):
    """Handle a GET /user/{id} request."""
    user = get_user(user_id)
    if user is None:
        raise KeyError(f"User {user_id!r} not found")
    return user.name
