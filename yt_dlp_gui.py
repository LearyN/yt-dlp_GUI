import os
import re
import sys
import json
import shutil
import threading
import queue
import subprocess
import tempfile
import zipfile
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def resource_path(rel_path: str) -> str:
    """
    Get absolute path to resource, works for dev and for PyInstaller onefile.
    """
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, rel_path)


def app_base_dir() -> str:
    """
    Directory where writable app files should live.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
DEST_RE = re.compile(r"\[download\]\s+Destination:\s+(.*)")


QUALITY_FORMATS = {
    "Best (recommended)": "bv*+ba/best",
    "2160p (4K)": "bv*[height<=2160]+ba/b[height<=2160]",
    "1440p (2K)": "bv*[height<=1440]+ba/b[height<=1440]",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "720p": "bv*[height<=720]+ba/b[height<=720]",
    "480p": "bv*[height<=480]+ba/b[height<=480]",
    "360p": "bv*[height<=360]+ba/b[height<=360]",
}

COOKIES_BROWSERS = [
    "chrome",
    "edge",
    "firefox",
    "brave",
    "chromium",
    "opera",
    "vivaldi",
]


DEFAULT_ARGS = [
    "-N", "4",
    "--js-runtime", "node",          # will auto-disable if node not found
    "--merge-output-format", "mp4",
    "--remux-video", "mp4",
    "--retries", "100",
    "--fragment-retries", "100",
    "--socket-timeout", "60",
    "--http-chunk-size", "10M",
    "--concurrent-fragments", "1",
    "--newline",
    "-o", "%(title)s [%(id)s].%(ext)s",
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("yt-dlp GUI (4K -> MP4)")
        self.geometry("900x620")

        self.proc = None
        self.worker_thread = None
        self.ffmpeg_thread = None
        self.q = queue.Queue()

        self.current_file = tk.StringVar(value="(none)")
        self.out_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop"))
        self.threads = tk.StringVar(value="4")
        self.quality = tk.StringVar(value="Best (recommended)")
        self.login_mode = tk.StringVar(value="No login")
        self.cookies_browser = tk.StringVar(value="chrome")
        self.cookies_file = tk.StringVar(value="")
        self.progress = tk.DoubleVar(value=0.0)

        self._build_ui()
        self._check_tools_on_start()
        self.after(100, self._drain_queue)

    def _build_ui(self):
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        # URLs
        ttk.Label(frm, text="URLs (one per line):").grid(row=0, column=0, sticky="w")
        self.txt_urls = tk.Text(frm, height=10, wrap="word")
        self.txt_urls.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(6, 10))

        # Output dir
        ttk.Label(frm, text="Output folder:").grid(row=2, column=0, sticky="w")
        ent_out = ttk.Entry(frm, textvariable=self.out_dir)
        ent_out.grid(row=2, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(frm, text="Browse...", command=self._choose_out_dir).grid(row=2, column=2, sticky="ew")
        ttk.Button(frm, text="Open folder", command=self._open_out_dir).grid(row=2, column=3, sticky="ew")

        # Threads
        ttk.Label(frm, text="Download threads (-N):").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frm, textvariable=self.threads, width=8).grid(row=3, column=1, sticky="w", padx=(6, 0), pady=(8, 0))

        # Quality selector
        ttk.Label(frm, text="Video quality:").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.cmb_quality = ttk.Combobox(
            frm,
            textvariable=self.quality,
            values=list(QUALITY_FORMATS.keys()),
            state="readonly",
        )
        self.cmb_quality.grid(row=4, column=1, sticky="w", padx=(6, 0), pady=(8, 0))

        # Login mode
        ttk.Label(frm, text="YouTube login:").grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.cmb_login = ttk.Combobox(
            frm,
            textvariable=self.login_mode,
            values=["No login", "Use browser cookies", "Use cookies.txt file"],
            state="readonly",
        )
        self.cmb_login.grid(row=5, column=1, sticky="w", padx=(6, 0), pady=(8, 0))
        self.cmb_login.bind("<<ComboboxSelected>>", lambda _e: self._update_login_ui())

        # Browser cookies option
        ttk.Label(frm, text="Browser:").grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.cmb_browser = ttk.Combobox(
            frm,
            textvariable=self.cookies_browser,
            values=COOKIES_BROWSERS,
            state="readonly",
        )
        self.cmb_browser.grid(row=6, column=1, sticky="w", padx=(6, 0), pady=(8, 0))

        # cookies.txt option
        ttk.Label(frm, text="cookies.txt:").grid(row=7, column=0, sticky="w", pady=(8, 0))
        self.ent_cookie_file = ttk.Entry(frm, textvariable=self.cookies_file)
        self.ent_cookie_file.grid(row=7, column=1, sticky="ew", padx=(6, 6), pady=(8, 0))
        self.btn_cookie_file = ttk.Button(frm, text="Browse...", command=self._choose_cookies_file)
        self.btn_cookie_file.grid(row=7, column=2, sticky="ew", pady=(8, 0))

        # Tool status
        ttk.Label(frm, text="Tool status:").grid(row=8, column=0, sticky="w", pady=(8, 0))
        self.lbl_tools = ttk.Label(frm, text="", foreground="#444")
        self.lbl_tools.grid(row=8, column=1, columnspan=3, sticky="w", pady=(8, 0), padx=(6, 0))

        # Progress
        ttk.Label(frm, text="Current file:").grid(row=9, column=0, sticky="w", pady=(12, 0))
        ttk.Label(frm, textvariable=self.current_file).grid(row=9, column=1, columnspan=3, sticky="w", pady=(12, 0), padx=(6, 0))

        pb = ttk.Progressbar(frm, variable=self.progress, maximum=100)
        pb.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(6, 0))

        # Buttons
        btn_row = ttk.Frame(frm)
        btn_row.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        self.btn_start = ttk.Button(btn_row, text="Start download", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btn_row, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_update_ffmpeg = ttk.Button(btn_row, text="Update ffmpeg", command=self._update_ffmpeg_clicked)
        self.btn_update_ffmpeg.pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Clear log", command=self._clear_log).pack(side="left", padx=(8, 0))

        # Log
        ttk.Label(frm, text="Log:").grid(row=12, column=0, sticky="w", pady=(12, 0))
        self.txt_log = tk.Text(frm, height=16, wrap="word")
        self.txt_log.grid(row=13, column=0, columnspan=4, sticky="nsew", pady=(6, 0))

        # Layout weights
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(1, weight=1)
        frm.rowconfigure(13, weight=1)
        self._update_login_ui()

    def _choose_out_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get())
        if d:
            self.out_dir.set(d)

    def _open_out_dir(self):
        d = self.out_dir.get()
        if os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showerror("Error", f"Folder not found:\n{d}")

    def _choose_cookies_file(self):
        p = filedialog.askopenfilename(
            title="Select cookies file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if p:
            self.cookies_file.set(p)

    def _update_login_ui(self):
        mode = self.login_mode.get()
        browser_enabled = "readonly" if mode == "Use browser cookies" else "disabled"
        cookie_enabled = "normal" if mode == "Use cookies.txt file" else "disabled"
        button_enabled = "normal" if mode == "Use cookies.txt file" else "disabled"
        self.cmb_browser.configure(state=browser_enabled)
        self.ent_cookie_file.configure(state=cookie_enabled)
        self.btn_cookie_file.configure(state=button_enabled)

    def _log(self, s: str):
        self.txt_log.insert("end", s + "\n")
        self.txt_log.see("end")

    def _clear_log(self):
        self.txt_log.delete("1.0", "end")

    def _find_tools(self):
        bin_dir = os.path.join(app_base_dir(), "bin")
        yt_local = os.path.join(bin_dir, "yt-dlp.exe")
        ff_local = os.path.join(bin_dir, "ffmpeg.exe")
        yt_embedded = resource_path(os.path.join("bin", "yt-dlp.exe"))
        ff_embedded = resource_path(os.path.join("bin", "ffmpeg.exe"))

        yt = None
        ff = None
        if os.path.exists(yt_local):
            yt = yt_local
        elif os.path.exists(yt_embedded):
            yt = yt_embedded
        else:
            yt = shutil.which("yt-dlp")

        if os.path.exists(ff_local):
            ff = ff_local
        elif os.path.exists(ff_embedded):
            ff = ff_embedded
        else:
            ff = shutil.which("ffmpeg")

        node = shutil.which("node")

        return yt, ff, node

    def _update_ffmpeg_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Info", "A download task is running. Please update ffmpeg after it finishes.")
            return
        if self.ffmpeg_thread and self.ffmpeg_thread.is_alive():
            messagebox.showinfo("Info", "ffmpeg update is already running.")
            return

        self.btn_update_ffmpeg.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self._log("Updating ffmpeg.exe from latest GitHub release...")
        self.ffmpeg_thread = threading.Thread(target=self._download_ffmpeg_worker, daemon=True)
        self.ffmpeg_thread.start()

    def _download_ffmpeg_worker(self):
        api_url = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
        headers = {"User-Agent": "yt-dlp-gui"}
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                release = json.loads(resp.read().decode("utf-8"))

            assets = release.get("assets", [])
            chosen = None
            for a in assets:
                name = a.get("name", "")
                if name.endswith("win64-gpl.zip") and "latest" in name and "shared" not in name:
                    chosen = a
                    break
            if not chosen:
                for a in assets:
                    name = a.get("name", "")
                    if name.endswith("win64-gpl-shared.zip") and "latest" in name:
                        chosen = a
                        break
            if not chosen:
                raise RuntimeError("Could not find a suitable ffmpeg Windows x64 build.")

            download_url = chosen.get("browser_download_url")
            if not download_url:
                raise RuntimeError("Release asset has no download URL.")

            with tempfile.TemporaryDirectory() as td:
                zip_path = os.path.join(td, "ffmpeg.zip")
                req_asset = urllib.request.Request(download_url, headers=headers)
                with urllib.request.urlopen(req_asset, timeout=300) as resp, open(zip_path, "wb") as out:
                    shutil.copyfileobj(resp, out)

                with zipfile.ZipFile(zip_path, "r") as zf:
                    ffmpeg_member = None
                    for member in zf.namelist():
                        if member.endswith("/bin/ffmpeg.exe"):
                            ffmpeg_member = member
                            break
                    if not ffmpeg_member:
                        raise RuntimeError("ffmpeg.exe not found inside downloaded archive.")

                    bin_dir = os.path.join(app_base_dir(), "bin")
                    os.makedirs(bin_dir, exist_ok=True)
                    target_path = os.path.join(bin_dir, "ffmpeg.exe")
                    tmp_target = os.path.join(bin_dir, "ffmpeg.exe.new")
                    with zf.open(ffmpeg_member) as src, open(tmp_target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(tmp_target, target_path)

            tag = release.get("tag_name", "unknown")
            self.q.put(("log", f"ffmpeg update complete: {tag}"))
            self.q.put(("ffmpeg_done", (True, "")))
        except Exception as e:
            self.q.put(("log", f"ffmpeg update failed: {e}"))
            self.q.put(("ffmpeg_done", (False, str(e))))

    def _check_tools_on_start(self):
        yt, ff, node = self._find_tools()
        yt_version = ""
        if yt:
            try:
                yt_version = subprocess.check_output([yt, "--version"], text=True).strip()
            except Exception:
                yt_version = ""

        parts = [
            f"yt-dlp: {'OK ' + yt_version if yt_version else ('OK' if yt else 'NOT FOUND')}",
            f"ffmpeg: {'OK' if ff else 'NOT FOUND'}",
            f"node: {'OK' if node else 'NOT FOUND'}",
        ]
        self.lbl_tools.configure(text=" | ".join(parts))

        if not yt:
            self._log("ERROR: yt-dlp not found. Put bin\\yt-dlp.exe next to this script or add yt-dlp to PATH.")
        if not ff:
            self._log("ERROR: ffmpeg not found. Auto-merge requires ffmpeg. Put bin\\ffmpeg.exe next to this script or add ffmpeg to PATH.")
        if not node:
            self._log("INFO: node not found. Will run without --js-runtime node (still works, but less future-proof).")

    def _get_urls(self):
        raw = self.txt_urls.get("1.0", "end").strip()
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Info", "Download already running.")
            return

        urls = self._get_urls()
        if not urls:
            messagebox.showerror("Error", "Please paste at least one URL.")
            return

        out_dir = self.out_dir.get().strip()
        if not out_dir:
            messagebox.showerror("Error", "Please choose an output folder.")
            return
        os.makedirs(out_dir, exist_ok=True)

        yt, ff, node = self._find_tools()
        if not yt:
            messagebox.showerror("Error", "yt-dlp not found (bin\\yt-dlp.exe missing and not in PATH).")
            return
        if not ff:
            messagebox.showerror(
                "Error",
                "ffmpeg not found. Auto-merge needs ffmpeg.\n"
                "Please place bin\\ffmpeg.exe next to this script or add ffmpeg to PATH.",
            )
            return

        # Build args
        args = DEFAULT_ARGS.copy()

        # set -N from UI
        try:
            n = int(self.threads.get().strip())
            if n < 1:
                raise ValueError
        except Exception:
            messagebox.showerror("Error", "Threads (-N) must be a positive integer.")
            return

        if "-N" in args:
            i = args.index("-N")
            args[i + 1] = str(n)

        # set quality format from UI
        quality_key = self.quality.get()
        fmt = QUALITY_FORMATS.get(quality_key)
        if not fmt:
            messagebox.showerror("Error", "Please choose a valid video quality.")
            return
        args = ["-f", fmt] + args

        # optional login for YouTube anti-bot scenarios
        mode = self.login_mode.get()
        if mode == "Use browser cookies":
            browser = self.cookies_browser.get().strip().lower()
            if browser not in COOKIES_BROWSERS:
                messagebox.showerror("Error", "Please choose a valid browser for cookies.")
                return
            args = ["--cookies-from-browser", browser] + args
        elif mode == "Use cookies.txt file":
            cookie_path = self.cookies_file.get().strip()
            if not cookie_path:
                messagebox.showerror("Error", "Please choose a cookies.txt file.")
                return
            if not os.path.isfile(cookie_path):
                messagebox.showerror("Error", f"cookies file not found:\n{cookie_path}")
                return
            args = ["--cookies", cookie_path] + args

        # If node not installed, remove --js-runtime node
        if not node:
            try:
                j = args.index("--js-runtime")
                # remove flag + value
                del args[j:j + 2]
            except ValueError:
                pass

        # Make sure yt-dlp can find ffmpeg: pass its directory explicitly
        if ff:
            ff_dir = os.path.dirname(ff)
            if ff_dir:
                args = ["--ffmpeg-location", ff_dir] + args

        cmd = [yt] + args + urls

        self._log("CMD: " + " ".join(cmd))
        self._log(f"Output folder: {out_dir}")
        self.progress.set(0.0)
        self.current_file.set("(starting...)")

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        self.worker_thread = threading.Thread(target=self._run_process, args=(cmd, out_dir), daemon=True)
        self.worker_thread.start()

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self._log("Sent terminate signal.")
            except Exception as e:
                self._log(f"Stop error: {e}")

    def _run_process(self, cmd, out_dir):
        env = os.environ.copy()

        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=out_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                env=env,
            )
        except Exception as e:
            self.q.put(("log", f"Failed to start yt-dlp: {e}"))
            self.q.put(("done", -1))
            return

        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self.q.put(("log", line))

            m = DEST_RE.search(line)
            if m:
                self.q.put(("file", m.group(1)))

            pm = PROGRESS_RE.search(line)
            if pm:
                try:
                    self.q.put(("progress", float(pm.group(1))))
                except Exception:
                    pass

        code = self.proc.wait()
        self.q.put(("done", code))

    def _drain_queue(self):
        try:
            while True:
                typ, payload = self.q.get_nowait()
                if typ == "log":
                    self._log(payload)
                elif typ == "file":
                    self.current_file.set(payload)
                elif typ == "progress":
                    self.progress.set(payload)
                elif typ == "done":
                    code = payload
                    self._log("DONE: success." if code == 0 else f"DONE: exit code {code}")
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.current_file.set("(none)")
                    self.progress.set(0.0)
                elif typ == "ffmpeg_done":
                    ok, err = payload
                    self.btn_update_ffmpeg.configure(state="normal")
                    if not (self.worker_thread and self.worker_thread.is_alive()):
                        self.btn_start.configure(state="normal")
                    self._check_tools_on_start()
                    if ok:
                        messagebox.showinfo("Success", "ffmpeg.exe has been updated.")
                    else:
                        messagebox.showerror("Error", f"Failed to update ffmpeg:\n{err}")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)


if __name__ == "__main__":
    app = App()
    app.mainloop()
