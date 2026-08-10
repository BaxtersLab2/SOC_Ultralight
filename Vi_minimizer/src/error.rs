//! Error type for vi_minimizer.
//!
//! Deliberately platform-independent so the shared validation / quoting logic
//! in [`crate::util`] can be unit-tested on any host, not just Windows.

use std::fmt;

#[derive(Debug)]
pub enum ViError {
    /// Bad input or usage (empty name, illegal characters, no command, ...).
    Invalid(String),
    /// A Win32 API call failed. `code` is the raw HRESULT.
    Win32 {
        context: String,
        code: i32,
        message: String,
    },
    /// The launched child exited with an unexpected / non-success code.
    ChildFailed { code: u32 },
    /// An operation timed out.
    Timeout,
}

impl fmt::Display for ViError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ViError::Invalid(m) => write!(f, "invalid: {m}"),
            ViError::Win32 {
                context,
                code,
                message,
            } => write!(
                f,
                "win32 {context} failed (0x{:08x}): {message}",
                *code as u32
            ),
            ViError::ChildFailed { code } => {
                write!(f, "child process failed with code {code}")
            }
            ViError::Timeout => write!(f, "operation timed out"),
        }
    }
}

impl std::error::Error for ViError {}

pub type Result<T> = std::result::Result<T, ViError>;

/// Convert a `windows::core::Error` into a [`ViError::Win32`] with context.
#[cfg(windows)]
pub(crate) fn win_err(context: &str, e: windows::core::Error) -> ViError {
    ViError::Win32 {
        context: context.to_string(),
        code: e.code().0,
        message: e.message(),
    }
}
