//! A second exe for the test plan. As a scheduled job (no ANCI_PORT) it records
//! one tick in the `jobs` db and exits; as a service it answers HTTP with the
//! tick count, so the same binary can switch between the two.

use std::env;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::Command;

fn main() {
    let db = env::var("ANCI_DB_JOBS_PATH").unwrap_or_default();
    match env::var("ANCI_PORT") {
        Err(_) => {
            let out = sqlite(&db, "INSERT INTO ticks (at) VALUES (datetime('now')); SELECT count(*) FROM ticks;");
            println!("ticks: {out}");
        }
        Ok(port) => {
            let bind = env::var("ANCI_BIND").unwrap_or_else(|_| "127.0.0.1".into());
            let listener = TcpListener::bind(format!("{bind}:{port}")).expect("bind");
            eprintln!("tick listening on {bind}:{port}");
            for mut stream in listener.incoming().flatten() {
                let mut buf = [0u8; 4096];
                let _ = stream.read(&mut buf);
                let body = format!("ticks: {}\n", sqlite(&db, "SELECT count(*) FROM ticks;"));
                let _ = write!(stream, "HTTP/1.1 200 OK\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}", body.len());
            }
        }
    }
}

fn sqlite(db: &str, sql: &str) -> String {
    if db.is_empty() {
        return "no db (ANCI_DB_JOBS_PATH isn't set)".into();
    }
    match Command::new("sqlite3").arg(db).arg(sql).output() {
        Ok(out) if out.status.success() => String::from_utf8_lossy(&out.stdout).trim().to_string(),
        Ok(out) => format!("sqlite3 failed: {}", String::from_utf8_lossy(&out.stderr).trim()),
        Err(e) => format!("couldn't run sqlite3: {e}"),
    }
}
