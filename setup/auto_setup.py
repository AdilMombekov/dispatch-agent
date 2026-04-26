"""First-run setup: generates token, scans apps, asks for Railway URL."""
import sys
import os

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from agent.config import ensure_config, save_config, detect_app_paths, get_chrome_profiles


def run_setup():
    print("=== Dispatch Agent — First Run Setup ===\n")

    cfg = ensure_config()

    print(f"Agent Token: {cfg['agent_token']}")
    print("(Copy this token into your n8n AGENT_TOKEN credential)\n")

    railway_url = input("Enter your Railway n8n URL (e.g. https://your-app.railway.app): ").strip()
    if railway_url:
        cfg["railway_url"] = railway_url.rstrip("/")

    print("\nScanning for installed apps...")
    cfg["apps"] = detect_app_paths()
    for app, path in cfg["apps"].items():
        status = path if path else "NOT FOUND"
        print(f"  {app:20s} {status}")

    print("\nScanning Chrome profiles...")
    profiles = get_chrome_profiles()
    if profiles:
        for p in profiles:
            print(f"  [{p['directory']}] {p['display']}")
    else:
        print("  No Chrome profiles found.")

    autostart = input("\nEnable autostart on Windows login? [y/N]: ").strip().lower()
    cfg["autostart"] = autostart == "y"

    if cfg["autostart"]:
        _enable_autostart()

    save_config(cfg)
    print("\nConfig saved to config.json")
    print("\nSetup complete! Run: python main.py")


def _enable_autostart():
    import winreg
    import sys

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    exe = sys.executable
    script = str(__import__("pathlib").Path(__file__).parent.parent / "main.py")
    value = f'"{exe}" "{script}"'
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "DispatchAgent", 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        print("Autostart enabled.")
    except Exception as e:
        print(f"Could not enable autostart: {e}")


if __name__ == "__main__":
    run_setup()
