import os
import re
import sys
import shutil
import threading
import queue
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def resource_path(rel_path: str) -> str:
    """
    Get absolute path to resource, works for dev and for PyInstaller onefile.
    """
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, rel_path)


PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
DEST_RE = re.compile(r"\[download\]\s+Destination:\s+(.*)")


DEFAULT_ARGS = [
    "-N", "4",
    "-f", "bv*+ba/best",
    "--js-runtime", "node",          # will auto-disable if node not found
    "--merge-output-format", "mp4",
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
        self.q = queue.Queue()

        self.current_file = tk.StringVar(value="(none)")
        self.out_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop"))
        self.threads = tk.StringVar(value="4")
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

        # Tool status
        ttk.Label(frm, text="Tool status:").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.lbl_tools = ttk.Label(frm, text="", foreground="#444")
        self.lbl_tools.grid(row=4, column=1, columnspan=3, sticky="w", pady=(8, 0), padx=(6, 0))

        # Progress
        ttk.Label(frm, text="Current file:").grid(row=5, column=0, sticky="w", pady=(12, 0))
        ttk.Label(frm, textvariable=self.current_file).grid(row=5, column=1, columnspan=3, sticky="w", pady=(12, 0), padx=(6, 0))

        pb = ttk.Progressbar(frm, variable=self.progress, maximum=100)
        pb.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(6, 0))

        # Buttons
        btn_row = ttk.Frame(frm)
        btn_row.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        self.btn_start = ttk.Button(btn_row, text="Start download", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btn_row, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Clear log", command=self._clear_log).pack(side="left", padx=(8, 0))

        # Log
        ttk.Label(frm, text="Log:").grid(row=8, column=0, sticky="w", pady=(12, 0))
        self.txt_log = tk.Text(frm, height=16, wrap="word")
        self.txt_log.grid(row=9, column=0, columnspan=4, sticky="nsew", pady=(6, 0))

        # Layout weights
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(1, weight=1)
        frm.rowconfigure(9, weight=1)

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

    def _log(self, s: str):
        self.txt_log.insert("end", s + "\n")
        self.txt_log.see("end")

    def _clear_log(self):
        self.txt_log.delete("1.0", "end")

    def _find_tools(self):
        yt_local = resource_path(os.path.join("bin", "yt-dlp.exe"))
        ff_local = resource_path(os.path.join("bin", "ffmpeg.exe"))

        yt = yt_local if os.path.exists(yt_local) else shutil.which("yt-dlp")
        ff = ff_local if os.path.exists(ff_local) else shutil.which("ffmpeg")
        node = shutil.which("node")

        return yt, ff, node

    def _check_tools_on_start(self):
        yt, ff, node = self._find_tools()

        parts = [
            f"yt-dlp: {'OK' if yt else 'NOT FOUND'}",
            f"ffmpeg: {'OK' if ff else 'NOT FOUND'}",
            f"node: {'OK' if node else 'NOT FOUND'}",
        ]
        self.lbl_tools.configure(text=" | ".join(parts))

        if not yt:
            self._log("ERROR: yt-dlp not found. Put bin\\yt-dlp.exe next to this script or add yt-dlp to PATH.")
        if not ff:
            self._log("WARNING: ffmpeg not found. Put bin\\ffmpeg.exe next to this script or add ffmpeg to PATH.")
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

        # If node not installed, remove --js-runtime node
        if not node:
            try:
                j = args.index("--js-runtime")
                # remove flag + value
                del args[j:j + 2]
            except ValueError:
                pass

        # Make sure yt-dlp can find ffmpeg: best is to pass --ffmpeg-location when we have a local ffmpeg
        if ff and os.path.basename(ff).lower() == "ffmpeg.exe":
            # ff might be a full path; pass its directory
            ff_dir = os.path.dirname(ff)
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
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)


if __name__ == "__main__":
    app = App()
    app.mainloop()
