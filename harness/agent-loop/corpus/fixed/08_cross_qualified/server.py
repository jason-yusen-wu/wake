import parser

def start_server(port_text):
    port = parser.parse_port(port_text)
    if port is None:
        raise ValueError(f"Invalid port: {port_text!r}")
    return port.to_bytes(2, "big")
