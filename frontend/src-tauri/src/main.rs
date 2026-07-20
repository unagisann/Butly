// Windows の release build でコンソールを出さない
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    butly_desktop_lib::run()
}
