def process_record(record_id):
    record = fetch_record(record_id)
    if record is None:
        return ""
    return record["title"]
