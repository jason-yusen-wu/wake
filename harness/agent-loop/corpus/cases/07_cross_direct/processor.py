def process_record(record_id):
    record = fetch_record(record_id)
    return record["title"]
