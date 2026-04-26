"""Build script — packages the agent into a single .exe via PyInstaller."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def build():
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", "DispatchAgent",
        "--icon", str(ROOT / "assets" / "icon.ico") if (ROOT / "assets" / "icon.ico").exists() else "NONE",
        "--add-data", f"{ROOT / 'agent'};agent",
        "--add-data", f"{ROOT / 'ui'};ui",
        "--hidden-import", "pystray._win32",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "customtkinter",
        "--hidden-import", "mss",
        "--hidden-import", "psutil",
        str(ROOT / "main.py"),
    ]

    print("Running PyInstaller...")
    print(" ".join(args))
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode == 0:
        exe = ROOT / "dist" / "DispatchAgent.exe"
        print(f"\nBuild success: {exe}")
    else:
        print("\nBuild FAILED")
        sys.exit(1)


if __name__ == "__main__":
    build()
