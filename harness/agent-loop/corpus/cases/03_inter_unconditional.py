def fetch_config():
    return None

def load_settings():
    config = fetch_config()
    return config["debug"]
