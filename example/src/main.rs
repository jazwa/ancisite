//! A tiny anci example: an HTTP server (std only) that records each visit in
//! the `main` db through the box's sqlite3 and shows what anci handed it.

use std::env;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::Command;

fn main() {
    let bind = env::var("ANCI_BIND").unwrap_or_else(|_| "127.0.0.1".into());
    let port = env::var("ANCI_PORT").unwrap_or_else(|_| "8000".into());
    let listener = TcpListener::bind(format!("{bind}:{port}")).expect("bind");
    eprintln!("listening on {bind}:{port}");
    for stream in listener.incoming().flatten() {
        handle(stream);
    }
}

fn handle(mut stream: TcpStream) {
    let mut buf = [0u8; 4096];
    let n = stream.read(&mut buf).unwrap_or(0);
    let request = String::from_utf8_lossy(&buf[..n]);
    let path = request.split_whitespace().nth(1).unwrap_or("/");

    let body = if path == "/" { page() } else { String::from("not found\n") };
    let status = if path == "/" { "200 OK" } else { "404 Not Found" };
    let _ = write!(
        stream,
        "HTTP/1.1 {status}\r\ncontent-type: text/plain; charset=utf-8\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    );
}

fn page() -> String {
    let anci_env = env::var("ANCI_ENV").unwrap_or_else(|_| "?".into());
    let db = env::var("ANCI_DB_MAIN_PATH").unwrap_or_default();
    let visits = if db.is_empty() {
        "no db (ANCI_DB_MAIN_PATH isn't set)".to_string()
    } else {
        sqlite(&db, "INSERT INTO visits (at) VALUES (datetime('now')); SELECT count(*) FROM visits;")
    };
    format!("hello from anci ({anci_env})\ndb: {db}\nvisits: {visits}\n")
}

/// Run SQL against the db with the box's sqlite3 (anci ships it).
fn sqlite(db: &str, sql: &str) -> String {
    match Command::new("sqlite3").arg(db).arg(sql).output() {
        Ok(out) if out.status.success() => String::from_utf8_lossy(&out.stdout).trim().to_string(),
        Ok(out) => format!("sqlite3 failed: {}", String::from_utf8_lossy(&out.stderr).trim()),
        Err(e) => format!("couldn't run sqlite3: {e}"),
    }
}
