//! Platform-independent helpers: validation, UTF-16 conversion, and Windows
//! command-line quoting. Unit-tested on any host.

use crate::error::{Result, ViError};

/// Null-terminated UTF-16, ready for the Win32 `*W` APIs.
pub fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Validate a desktop-object name before handing it to `CreateDesktopW`.
///
/// Desktop names live in the window-station namespace and may not contain a
/// backslash (the station/desktop separator) or control characters.
pub fn validate_desktop_name(name: &str) -> Result<()> {
    if name.is_empty() {
        return Err(ViError::Invalid("desktop name is empty".into()));
    }
    if name.chars().count() > 128 {
        return Err(ViError::Invalid(
            "desktop name too long (max 128 chars)".into(),
        ));
    }
    if name.contains('\\') {
        return Err(ViError::Invalid("desktop name cannot contain '\\'".into()));
    }
    if name.chars().any(|c| c.is_control()) {
        return Err(ViError::Invalid(
            "desktop name cannot contain control characters".into(),
        ));
    }
    Ok(())
}

/// Build a Windows command line from an argv slice using the standard
/// `CommandLineToArgvW` quoting rules, so what we launch round-trips back to
/// the same argument vector the caller passed.
pub fn build_command_line(args: &[String]) -> String {
    let mut out = String::new();
    for (i, arg) in args.iter().enumerate() {
        if i > 0 {
            out.push(' ');
        }
        let needs_quote = arg.is_empty()
            || arg.contains(|c: char| c == ' ' || c == '\t' || c == '\n' || c == '"');
        if !needs_quote {
            out.push_str(arg);
            continue;
        }
        out.push('"');
        let mut backslashes = 0usize;
        for c in arg.chars() {
            match c {
                '\\' => backslashes += 1,
                '"' => {
                    // Escape all pending backslashes, then the quote itself.
                    for _ in 0..(backslashes * 2 + 1) {
                        out.push('\\');
                    }
                    backslashes = 0;
                    out.push('"');
                }
                _ => {
                    for _ in 0..backslashes {
                        out.push('\\');
                    }
                    backslashes = 0;
                    out.push(c);
                }
            }
        }
        // Backslashes immediately before the closing quote must be doubled.
        for _ in 0..(backslashes * 2) {
            out.push('\\');
        }
        out.push('"');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wide_is_null_terminated() {
        assert_eq!(wide("AB"), vec![0x41, 0x42, 0x00]);
        assert_eq!(wide(""), vec![0x00]);
    }

    #[test]
    fn rejects_bad_names() {
        assert!(validate_desktop_name("").is_err());
        assert!(validate_desktop_name("a\\b").is_err());
        assert!(validate_desktop_name("bad\u{7}bell").is_err());
        assert!(validate_desktop_name(&"x".repeat(129)).is_err());
    }

    #[test]
    fn accepts_good_names() {
        assert!(validate_desktop_name("soc_vi").is_ok());
        assert!(validate_desktop_name("Vi Minimizer 1").is_ok());
    }

    #[test]
    fn quotes_only_when_needed() {
        assert_eq!(
            build_command_line(&[
                "cmd.exe".into(),
                "/c".into(),
                "exit".into(),
                "7".into()
            ]),
            "cmd.exe /c exit 7"
        );
    }

    #[test]
    fn quotes_spaces() {
        assert_eq!(build_command_line(&["a b".into()]), "\"a b\"");
    }

    #[test]
    fn quotes_embedded_quote() {
        // a"b  ->  "a\"b"
        assert_eq!(build_command_line(&["a\"b".into()]), "\"a\\\"b\"");
    }

    #[test]
    fn doubles_backslashes_before_closing_quote() {
        // C:\a b\  ->  "C:\a b\\"
        assert_eq!(
            build_command_line(&["C:\\a b\\".into()]),
            "\"C:\\a b\\\\\""
        );
    }

    #[test]
    fn leaves_lone_backslash_unquoted() {
        // a\  b  ->  a\ b   (no space inside the first arg)
        assert_eq!(build_command_line(&["a\\".into(), "b".into()]), "a\\ b");
    }
}
