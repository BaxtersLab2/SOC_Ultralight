//! Live integration test of the isolation core (Windows-only).
//!
//! Creates a real, separate Win32 desktop, launches a child on it, and confirms
//! the exit code round-trips — proving a process can be run on an isolated
//! desktop without touching the active one.

#![cfg(windows)]

use vi_minimizer::{launch_on_desktop, VirtualDesktop, WaitOutcome};

#[test]
fn creates_isolated_desktop_and_runs_child() {
    let name = "vi_it_selftest";
    let desk = VirtualDesktop::create(name).expect("create isolated desktop");
    assert_eq!(desk.name(), name);

    let child = launch_on_desktop(
        name,
        &[
            "cmd.exe".to_string(),
            "/c".to_string(),
            "exit".to_string(),
            "7".to_string(),
        ],
        None,
    )
    .expect("launch child on isolated desktop");
    assert!(child.pid != 0, "child should have a real pid");

    match child.wait(15_000).expect("wait for child") {
        WaitOutcome::Exited(code) => assert_eq!(code, 7, "child on isolated desktop should exit 7"),
        WaitOutcome::Timeout => panic!("child on isolated desktop timed out"),
    }

    drop(child);
    drop(desk);
}

#[test]
fn rejects_invalid_desktop_name() {
    assert!(VirtualDesktop::create("bad\\name").is_err());
    assert!(VirtualDesktop::create("").is_err());
}

#[test]
fn enumerates_and_tears_down_windows_on_isolated_desktop() {
    use vi_minimizer::{list_windows, shutdown_desktop};
    use std::time::Duration;

    let name = "vi_it_lifecycle";
    let desk = VirtualDesktop::create(name).expect("create isolated desktop");

    // Launch a GUI app that opens a real top-level window on the hidden desktop.
    let child =
        launch_on_desktop(name, &["notepad.exe".to_string()], None).expect("launch notepad");

    // Wait for its window to appear (window creation is async).
    let mut found = false;
    for _ in 0..50 {
        let ws = list_windows(name).expect("list windows");
        if ws.iter().any(|w| w.pid == child.pid) {
            found = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(found, "notepad window should appear on the isolated desktop");

    // Tear the whole desktop down.
    let report = shutdown_desktop(name).expect("shutdown desktop");
    assert!(
        report.terminated.contains(&child.pid),
        "notepad pid {} should have been terminated (report: {:?})",
        child.pid,
        report
    );

    // Give the OS a moment, then confirm the window is gone.
    std::thread::sleep(Duration::from_millis(400));
    let after = list_windows(name).expect("list after shutdown");
    assert!(
        !after.iter().any(|w| w.pid == child.pid),
        "notepad should be gone after shutdown"
    );

    drop(child);
    drop(desk);
}

#[test]
fn refuses_to_shutdown_default_desktop() {
    use vi_minimizer::shutdown_desktop;
    // Safety rail: must never nuke the operator's real apps.
    assert!(shutdown_desktop("Default").is_err());
    assert!(shutdown_desktop("default").is_err());
}
