"""S-meter GUI using Tkinter (runs in separate thread)."""

import queue
import threading
import time
import tkinter as tk
from typing import List, Tuple


class SMeterDisplay:
    """Tkinter S-meter that updates asynchronously via a queue."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._command_queue: queue.Queue = queue.Queue()  # for toggle commands
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._samples: List[Tuple[float, float]] = []  # (timestamp, dbm)
        self._avg_window = 0.7  # seconds

        # GUI elements (created in _run)
        self._root: Optional[tk.Tk] = None
        self._canvas: Optional[tk.Canvas] = None
        self._bar: Optional[int] = None
        self._dbm_label: Optional[tk.Label] = None
        self._s_inst_label: Optional[tk.Label] = None
        self._s_avg_label: Optional[tk.Label] = None

    def start(self) -> None:
        """Start the Tkinter thread."""
        self._thread.start()

    def update_power(self, dbm: float) -> None:
        """Queue a new power sample (called from any thread)."""
        self._queue.put((time.time(), dbm))

    def toggle_visibility(self) -> None:
        """Queue a command to show/hide the S‑meter window."""
        self._command_queue.put(lambda: self._toggle())

    # ========== Conversion utilities ==========

    @staticmethod
    def dbm_to_s(dbm: float) -> float:
        """Convert dBm to S-unit (approx)."""
        s = 9 + (dbm + 73) / 6
        return max(0, s)

    @staticmethod
    def format_s(s: float) -> str:
        """Format S-value as string (e.g., 'S9', 'S9+20')."""
        if s <= 9:
            return f"S{int(round(s))}"
        over = (s - 9) * 6
        return f"S9+{int(over)}"

    @staticmethod
    def dbm_to_meter(dbm: float) -> float:
        """Map dBm to a 0..1 meter position."""
        # Clamp to typical S-meter range
        dbm = max(-121, min(-33, dbm))
        return (dbm + 121) / 88

    # ========== GUI loop ==========

    def _run(self) -> None:
        """Tkinter main loop (runs in separate thread)."""
        self._root = tk.Tk()
        self._root.overrideredirect(True)      # No title bar
        self._root.attributes("-topmost", True)
        self._root.resizable(False, False)

        # Set initial position to X=0, Y=300
        self._root.geometry("+0+300")

        # Make window draggable
        self._offset_x = 0
        self._offset_y = 0

        def start_move(event: tk.Event) -> None:
            self._offset_x = event.x
            self._offset_y = event.y

        def do_move(event: tk.Event) -> None:
            x = self._root.winfo_pointerx() - self._offset_x
            y = self._root.winfo_pointery() - self._offset_y
            self._root.geometry(f"+{x}+{y}")

        self._root.bind("<Button-1>", start_move)
        self._root.bind("<B1-Motion>", do_move)

        # Dark theme
        bg_main = "#2b2b2b"
        bg_meter = "#3a3a3a"
        text_color = "#d0d0d0"
        tick_color = "#9a9a9a"
        bar_color = "#4CAF50"

        self._root.configure(bg=bg_main)

        width = 420
        height = 20

        self._canvas = tk.Canvas(
            self._root,
            width=width,
            height=height,
            bg=bg_meter,
            highlightthickness=0
        )
        self._canvas.pack(padx=8, pady=6)

        self._bar = self._canvas.create_rectangle(
            0, 0, 0, height,
            fill=bar_color,
            width=0
        )

        # Draw scale ticks
        scale_dbm = [
            -121, -115, -109, -103, -97, -91, -85, -79, -73,
            -67, -61, -55, -49, -43, -37, -33
        ]
        for dbm in scale_dbm:
            x = self.dbm_to_meter(dbm) * width
            self._canvas.create_line(
                x, 10, x, height,
                fill=tick_color
            )

        # Labels for S-units
        labels = [
            "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9",
            "+10", "+20", "+30", "+40", "+50", "+60"
        ]
        for i, label in enumerate(labels):
            dbm = scale_dbm[i + 1]  # skip first tick (S1 at -121?)
            x = self.dbm_to_meter(dbm) * width
            self._canvas.create_text(
                x, 6,
                text=label,
                fill=text_color,
                font=("Segoe UI", 8)
            )

        # Info frame
        info = tk.Frame(self._root, bg=bg_main)
        info.pack(pady=(2, 6))

        self._dbm_label = tk.Label(
            info,
            text="-120 dBm",
            font=("Segoe UI", 11),
            fg=text_color,
            bg=bg_main
        )
        self._dbm_label.pack(side="left", padx=12)

        self._s_inst_label = tk.Label(
            info,
            text="S0",
            font=("Segoe UI", 11),
            fg=text_color,
            bg=bg_main
        )
        self._s_inst_label.pack(side="left", padx=12)

        self._s_avg_label = tk.Label(
            info,
            text="S0 avg",
            font=("Segoe UI", 11),
            fg=text_color,
            bg=bg_main
        )
        self._s_avg_label.pack(side="left", padx=12)

        self._update_loop()
        self._root.mainloop()

    def _toggle(self) -> None:
        """Actually toggle visibility (runs in Tkinter thread)."""
        if not self._root:
            return
        if self._root.state() == "normal":
            self._root.withdraw()
        else:
            self._root.deiconify()

    def _update_loop(self) -> None:
        """Periodically update meter from queued samples and process commands."""
        # Process any pending commands
        try:
            while True:
                cmd = self._command_queue.get_nowait()
                cmd()
        except queue.Empty:
            pass

        now = time.time()
        # Drain power samples
        try:
            while True:
                t, dbm = self._queue.get_nowait()
                self._samples.append((t, dbm))
        except queue.Empty:
            pass

        # Remove old samples
        self._samples = [
            (t, v) for (t, v) in self._samples
            if now - t < self._avg_window
        ]

        if self._samples:
            inst_dbm = self._samples[-1][1]
            avg_dbm = sum(v for _, v in self._samples) / len(self._samples)

            s_inst = self.dbm_to_s(inst_dbm)
            s_avg = self.dbm_to_s(avg_dbm)

            self._dbm_label.config(text=f"{inst_dbm:.1f} dBm")
            self._s_inst_label.config(text=self.format_s(s_inst))
            self._s_avg_label.config(text=f"{self.format_s(s_avg)} avg")

            meter = self.dbm_to_meter(inst_dbm)
            width = int(meter * 420)
            self._canvas.coords(self._bar, 0, 0, width, 20)

        # Schedule next update (~25 fps)
        self._root.after(40, self._update_loop)