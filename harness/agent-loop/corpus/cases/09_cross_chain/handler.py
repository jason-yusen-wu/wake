def handle_get_user(user_id):
    """Handle a GET /user/{id} request."""
    user = get_user(user_id)
    return user.name
