def fetch_config():
    return None

def load_settings():
    config = fetch_config()
    if config is None:
        return False
    return config["debug"]
