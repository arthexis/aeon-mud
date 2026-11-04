//! Aeon MUD native console bootstrap.
//!
//! This is intentionally a real console subsystem executable, not a wrapper
//! around Python, PowerShell, Windows Terminal or the previous GUI project.
//! Telnet and persisted-profile behavior will be ported in subsequent steps.
use std::env;
use std::io::{self, IsTerminal, Write};
use std::process::ExitCode;

const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug, PartialEq, Eq)]
enum Command {
    Welcome,
    Help,
    Version,
    SelfTest,
    NotImplemented(String),
    Invalid(String),
}

fn parse(args: &[String]) -> Command {
    match args {
        [] => Command::Welcome,
        [flag] if matches!(flag.as_str(), "-h" | "--help" | "help") => Command::Help,
        [flag] if matches!(flag.as_str(), "-V" | "--version" | "version") => Command::Version,
        [flag] if flag == "self-test" => Command::SelfTest,
        [command, ..] if command == "connect" || command == "profile" => {
            Command::NotImplemented(command.clone())
        }
        [flag, ..] => Command::Invalid(flag.clone()),
    }
}

fn usage() -> &'static str {
    "Usage: aeon-mud [--help | --version | self-test]\n\
     \n\
     Aeon MUD Rust console experiment (native bootstrap only).\n\
     \n\
     Commands:\n\
       --help       Show this help\n\
       --version    Show the native executable version\n\
       self-test    Check the native executable starts correctly\n\
     \n\
     Telnet connections, profiles and DPAPI are not implemented in Rust yet.\n\
     Use the existing aeon-mud.bat / aeon-mud.ps1 Python client for gameplay."
}

fn execute(cmd: Command, interactive: bool, out: &mut impl Write, err: &mut impl Write) -> u8 {
    match cmd {
        Command::Welcome => {
            let _ = writeln!(out, "Aeon MUD — Rust console prototype v{VERSION}");
            let _ = writeln!(out, "Native console process; no Python or external terminal launcher.");
            let _ = writeln!(out, "\n{}", usage());
            if interactive {
                let _ = write!(out, "\nPress Enter to close this prototype...");
                let _ = out.flush();
                let mut line = String::new();
                let _ = io::stdin().read_line(&mut line);
            }
            0
        }
        Command::Help => {
            let _ = writeln!(out, "{}", usage());
            0
        }
        Command::Version => {
            let _ = writeln!(out, "aeon-mud {VERSION} (Rust console prototype)");
            0
        }
        Command::SelfTest => {
            let _ = writeln!(out, "Rust console self-test passed");
            0
        }
        Command::NotImplemented(name) => {
            let _ = writeln!(err, "The native '{name}' command is not implemented yet.");
            let _ = writeln!(err, "For working connections, use the Python client.");
            2
        }
        Command::Invalid(name) => {
            let _ = writeln!(err, "Unknown command: {name}\n\n{}", usage());
            2
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    let interactive = io::stdin().is_terminal() && io::stdout().is_terminal();
    ExitCode::from(execute(
        parse(&args),
        interactive,
        &mut io::stdout(),
        &mut io::stderr(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    #[test]
    fn help_aliases() {
        for value in ["help", "-h", "--help"] {
            assert_eq!(parse(&args(&[value])), Command::Help);
        }
    }

    #[test]
    fn version_and_self_test_commands() {
        assert_eq!(parse(&args(&["--version"])), Command::Version);
        assert_eq!(parse(&args(&["self-test"])), Command::SelfTest);
    }

    #[test]
    fn no_arguments_is_a_native_console_welcome() {
        assert_eq!(parse(&args(&[])), Command::Welcome);
        let mut out = Vec::new();
        let mut err = Vec::new();
        assert_eq!(execute(Command::Welcome, false, &mut out, &mut err), 0);
        assert!(String::from_utf8(out).unwrap().contains("Rust console prototype"));
        assert!(err.is_empty());
    }

    #[test]
    fn unimplemented_connection_fails_closed() {
        assert_eq!(
            parse(&args(&["connect", "A profile", "--login", "A Wizard"])),
            Command::NotImplemented("connect".to_string())
        );
        let mut out = Vec::new();
        let mut err = Vec::new();
        assert_eq!(
            execute(Command::NotImplemented("connect".into()), false, &mut out, &mut err),
            2
        );
        assert!(out.is_empty());
        assert!(String::from_utf8(err).unwrap().contains("not implemented"));
    }

    #[test]
    fn invalid_command_has_nonzero_exit() {
        let mut out = Vec::new();
        let mut err = Vec::new();
        assert_eq!(execute(parse(&args(&["unknown"])), false, &mut out, &mut err), 2);
        assert!(out.is_empty());
    }
}
