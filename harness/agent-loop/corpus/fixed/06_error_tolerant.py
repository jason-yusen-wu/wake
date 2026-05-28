def broken_helper(data):
    result = {k v for k, v in data}
    return result

def get_value():
    return None

def compute():
    val = get_value()
    if val is None:
        return 0
    return val.result
