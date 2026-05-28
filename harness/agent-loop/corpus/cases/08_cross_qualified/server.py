import parser

def start_server(port_text):
    port = parser.parse_port(port_text)
    return port.to_bytes(2, "big")
