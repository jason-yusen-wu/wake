use std::io::{self, BufRead, Write};
use wake_daemon::{parse_request, Daemon};

fn main() {
    let mut daemon = Daemon::default();
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = stdout.lock();

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) if l.trim().is_empty() => continue,
            Ok(l) => l,
            Err(_) => break,
        };

        let response = match parse_request(&line) {
            Ok(req) => daemon.handle(&req),
            Err(err_response) => err_response,
        };

        let json = serde_json::to_string(&response).unwrap_or_else(|_| "{}".to_string());
        writeln!(out, "{json}").ok();
        out.flush().ok();
    }
}
