"""Settings window — customtkinter dark UI with 4 tabs."""
import threading
import tkinter as tk
import winreg
import sys
from pathlib import Path

import customtkinter as ctk

from agent.config import load_config, save_config, detect_app_paths, get_chrome_profiles

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class SettingsWindow(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Dispatch Agent — Settings")
        self.geometry("600x520")
        self.resizable(False, False)

        self._cfg = load_config()
        self._build_ui()
        self._load_values()

    # ── Build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._tabs = ctk.CTkTabview(self, width=580, height=480)
        self._tabs.pack(padx=10, pady=10)

        self._tabs.add("Connection")
        self._tabs.add("Apps")
        self._tabs.add("Chrome")
        self._tabs.add("Autostart")

        self._build_connection_tab()
        self._build_apps_tab()
        self._build_chrome_tab()
        self._build_autostart_tab()

        save_btn = ctk.CTkButton(self, text="Save", command=self._on_save, width=120)
        save_btn.pack(pady=5)

    # ── Connection tab ─────────────────────────────────────────────────────

    def _build_connection_tab(self):
        tab = self._tabs.tab("Connection")

        ctk.CTkLabel(tab, text="Railway n8n URL:").grid(row=0, column=0, sticky="w", padx=10, pady=8)
        self._url_var = tk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._url_var, width=380).grid(row=0, column=1, pady=8)

        ctk.CTkLabel(tab, text="Agent Token:").grid(row=1, column=0, sticky="w", padx=10, pady=8)
        self._token_var = tk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._token_var, width=380).grid(row=1, column=1, pady=8)

        ctk.CTkLabel(tab, text="Poll interval (active, s):").grid(row=2, column=0, sticky="w", padx=10, pady=8)
        self._interval_active_var = tk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._interval_active_var, width=80).grid(row=2, column=1, sticky="w", pady=8)

        ctk.CTkLabel(tab, text="Poll interval (idle, s):").grid(row=3, column=0, sticky="w", padx=10, pady=8)
        self._interval_idle_var = tk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._interval_idle_var, width=80).grid(row=3, column=1, sticky="w", pady=8)

        test_btn = ctk.CTkButton(tab, text="Test Connection", command=self._test_connection, width=160)
        test_btn.grid(row=4, column=1, pady=12, sticky="w")
        self._conn_status = ctk.CTkLabel(tab, text="")
        self._conn_status.grid(row=5, column=0, columnspan=2, pady=4)

    # ── Apps tab ───────────────────────────────────────────────────────────

    def _build_apps_tab(self):
        tab = self._tabs.tab("Apps")
        self._app_vars: dict[str, tk.StringVar] = {}

        app_names = ["cursor", "vscode", "claude-code", "antigravity", "terminal", "chrome"]
        for i, app in enumerate(app_names):
            ctk.CTkLabel(tab, text=f"{app}:").grid(row=i, column=0, sticky="w", padx=10, pady=6)
            var = tk.StringVar()
            self._app_vars[app] = var
            ctk.CTkEntry(tab, textvariable=var, width=340).grid(row=i, column=1, pady=6)

        scan_btn = ctk.CTkButton(tab, text="Auto-Scan", command=self._scan_apps, width=120)
        scan_btn.grid(row=len(app_names), column=1, pady=10, sticky="w")

    # ── Chrome tab ─────────────────────────────────────────────────────────

    def _build_chrome_tab(self):
        tab = self._tabs.tab("Chrome")
        self._chrome_list = ctk.CTkTextbox(tab, width=530, height=360, state="normal")
        self._chrome_list.pack(padx=10, pady=10)
        refresh_btn = ctk.CTkButton(tab, text="Refresh Profiles", command=self._refresh_chrome, width=150)
        refresh_btn.pack(pady=5)

    # ── Autostart tab ─────────────────────────────────────────────────────

    def _build_autostart_tab(self):
        tab = self._tabs.tab("Autostart")
        self._autostart_var = tk.BooleanVar()
        ctk.CTkCheckBox(tab, text="Start with Windows", variable=self._autostart_var).pack(padx=20, pady=30)
        ctk.CTkLabel(tab, text="Adds this app to HKCU Run registry key.", text_color="gray").pack()

    # ── Load values ────────────────────────────────────────────────────────

    def _load_values(self):
        self._url_var.set(self._cfg.get("railway_url", ""))
        self._token_var.set(self._cfg.get("agent_token", ""))
        self._interval_active_var.set(str(self._cfg.get("poll_interval_active", 2)))
        self._interval_idle_var.set(str(self._cfg.get("poll_interval_idle", 10)))

        apps = self._cfg.get("apps", {})
        for app, var in self._app_vars.items():
            var.set(apps.get(app, ""))

        self._autostart_var.set(self._cfg.get("autostart", False))
        self._refresh_chrome()

    # ── Actions ────────────────────────────────────────────────────────────

    def _on_save(self):
        self._cfg["railway_url"] = self._url_var.get().strip().rstrip("/")
        self._cfg["agent_token"] = self._token_var.get().strip()
        try:
            self._cfg["poll_interval_active"] = int(self._interval_active_var.get())
        except ValueError:
            pass
        try:
            self._cfg["poll_interval_idle"] = int(self._interval_idle_var.get())
        except ValueError:
            pass

        for app, var in self._app_vars.items():
            self._cfg.setdefault("apps", {})[app] = var.get().strip()

        autostart = self._autostart_var.get()
        self._cfg["autostart"] = autostart
        _set_autostart(autostart)

        save_config(self._cfg)
        self.destroy()

    def _scan_apps(self):
        paths = detect_app_paths()
        for app, var in self._app_vars.items():
            var.set(paths.get(app, ""))

    def _refresh_chrome(self):
        profiles = get_chrome_profiles()
        self._chrome_list.configure(state="normal")
        self._chrome_list.delete("1.0", tk.END)
        if profiles:
            for p in profiles:
                self._chrome_list.insert(tk.END, f"[{p['directory']}]  {p['display']}\n")
        else:
            self._chrome_list.insert(tk.END, "No Chrome profiles found.\n")
        self._chrome_list.configure(state="disabled")

    def _test_connection(self):
        url = self._url_var.get().strip().rstrip("/")
        token = self._token_var.get().strip()
        if not url or not token:
            self._conn_status.configure(text="Enter URL and token first.", text_color="orange")
            return
        self._conn_status.configure(text="Testing...", text_color="gray")
        threading.Thread(target=self._do_test, args=(url, token), daemon=True).start()

    def _do_test(self, url: str, token: str):
        import requests
        try:
            resp = requests.get(
                f"{url}/webhook/agent-poll",
                headers={"X-Agent-Token": token},
                timeout=8,
            )
            if resp.status_code in (200, 204):
                self._conn_status.configure(text=f"Connected! HTTP {resp.status_code}", text_color="green")
            else:
                self._conn_status.configure(text=f"HTTP {resp.status_code}", text_color="orange")
        except Exception as e:
            self._conn_status.configure(text=f"Error: {e}", text_color="red")


# ── Autostart helper ───────────────────────────────────────────────────────

def _set_autostart(enabled: bool):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    exe = sys.executable
    script = str(Path(__file__).parent.parent / "main.py")
    value = f'"{exe}" "{script}"'
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, "DispatchAgent", 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(key, "DispatchAgent")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception:
        pass


def open_settings():
    win = SettingsWindow()
    win.mainloop()
