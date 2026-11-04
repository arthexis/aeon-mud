//! Contract fixture ingestion for the *future* Rust port.
//!
//! These tests validate the shared baseline format. They intentionally do not
//! claim native Telnet/ANSI/profile parity before those modules are ported.

fn rows(source: &str, columns: usize) -> Vec<Vec<&str>> {
    let mut names = std::collections::HashSet::new();
    let mut parsed = Vec::new();
    for line in source
        .lines()
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
    {
        let cols: Vec<_> = line.split('\t').collect();
        assert_eq!(cols.len(), columns, "invalid fixture columns: {line}");
        assert!(names.insert(cols[0]), "duplicate contract id: {}", cols[0]);
        parsed.push(cols);
    }
    parsed
}

fn is_hex(value: &str) -> bool {
    value.len().is_multiple_of(2) && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn check_hex(value: &str) {
    assert!(
        value == "-" || is_hex(value),
        "invalid hex fixture: {value}"
    );
}

#[test]
fn telnet_contract_is_shared_and_machine_readable() {
    let cases = rows(include_str!("../../contracts/v1/telnet.tsv"), 4);
    assert!(cases.len() >= 7);
    for row in cases {
        assert!(!row[0].is_empty());
        for chunk in row[1].split(',') {
            assert!(is_hex(chunk) && !chunk.is_empty());
        }
        check_hex(row[2]);
        check_hex(row[3]);
    }
}

#[test]
fn streaming_text_contract_is_shared_and_machine_readable() {
    let cases = rows(include_str!("../../contracts/v1/text.tsv"), 3);
    assert!(cases.len() >= 6);
    for row in cases {
        for chunk in row[1].split(',') {
            assert!(is_hex(chunk) && !chunk.is_empty());
        }
        assert!(is_hex(row[2]));
    }
}

#[test]
fn host_contract_is_shared_and_machine_readable() {
    let cases = rows(include_str!("../../contracts/v1/hosts.tsv"), 5);
    assert!(cases.len() >= 8);
    for row in cases {
        assert!(!row[0].is_empty() && !row[1].is_empty());
        if row[3] == "ERROR" {
            assert_eq!(row[4], "-");
        } else {
            assert!(row[4].parse::<u16>().is_ok());
            assert!(!row[3].is_empty());
        }
    }
}

#[test]
fn help_contract_is_shared_and_machine_readable() {
    let cases = rows(include_str!("../../contracts/v1/help.tsv"), 3);
    assert!(cases.len() >= 5);
    for row in cases {
        assert!(!row[0].is_empty());
        assert!(!row[1].is_empty() && !row[2].is_empty());
    }
}

#[test]
fn keyboard_contract_is_shared_and_machine_readable() {
    let cases = rows(include_str!("../../contracts/v1/keys.tsv"), 3);
    assert!(cases.len() >= 10);
    for row in cases {
        assert_eq!(row[1].len(), 2);
        assert!(is_hex(row[1]));
        assert!(!row[2].is_empty());
    }
}

#[test]
fn outbound_contract_is_shared_and_machine_readable() {
    let cases = rows(include_str!("../../contracts/v1/outbound.tsv"), 4);
    assert!(cases.len() >= 7);
    for row in cases {
        assert_eq!(row[1].chars().count(), 1);
        assert!(row[2].parse::<usize>().is_ok());
        assert!(row[3]
            .split(',')
            .all(|length| length.parse::<usize>().is_ok()));
    }
}

#[test]
fn identity_contract_is_shared_and_machine_readable() {
    let cases = rows(include_str!("../../contracts/v1/identity.tsv"), 5);
    assert!(cases.len() >= 4);
    for row in cases {
        assert!(row[2].parse::<u16>().is_ok());
        assert!(!row[3].is_empty());
        assert!(row[4].starts_with("character:["));
    }
}
