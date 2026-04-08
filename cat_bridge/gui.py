"""S-meter GUI with frequency offset bars (hidden by default)."""

import queue
import threading
import time
import tkinter as tk
from typing import Optional, List, Tuple


class SMeterDisplay:
    """Tkinter S-meter with split mode frequency offset bars."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._command_queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._samples: List[Tuple[float, float]] = []
        self._avg_window = 0.7

        self._root: Optional[tk.Tk] = None
        self._canvas: Optional[tk.Canvas] = None
        self._bar: Optional[int] = None
        self._dbm_label: Optional[tk.Label] = None
        self._s_inst_label: Optional[tk.Label] = None
        self._s_avg_label: Optional[tk.Label] = None

        # Offset indicator
        self._offset_canvas: Optional[tk.Canvas] = None
        self._tx_bar: Optional[int] = None
        self._rx_bar: Optional[int] = None
        self._yellow_bar: Optional[int] = None
        self._split_mode: int = 1
        self._rx_freq: int = 0
        self._tx_freq: int = 0

    def start(self) -> None:
        self._thread.start()

    def update_power(self, dbm: float) -> None:
        self._queue.put((time.time(), dbm))

    def toggle_visibility(self) -> None:
        self._command_queue.put(lambda: self._toggle())

    def update_split_display(self, mode: int, rx_freq: int, tx_freq: int) -> None:
        """Queue a split mode / frequency update from the main thread."""
        self._command_queue.put(lambda: self._update_split(mode, rx_freq, tx_freq))

    # ---------- Conversion utilities ----------
    @staticmethod
    def dbm_to_s(dbm: float) -> float:
        s = 9 + (dbm + 73) / 6
        return max(0, s)

    @staticmethod
    def format_s(s: float) -> str:
        if s <= 9:
            return f"S{int(round(s))}"
        over = (s - 9) * 6
        return f"S9+{int(over)}"

    @staticmethod
    def dbm_to_meter(dbm: float) -> float:
        dbm = max(-121, min(-33, dbm))
        return (dbm + 121) / 88

    # ---------- GUI construction ----------
    def _run(self) -> None:
        self._root = tk.Tk()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.resizable(False, False)
        self._root.geometry("+0+300")

        # Start hidden
        self._root.withdraw()

        # Dragging
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

        # S‑meter canvas
        width = 420
        height = 20

        self._canvas = tk.Canvas(
            self._root,
            width=width,
            height=height,
            bg=bg_meter,
            highlightthickness=0
        )
        self._canvas.pack(padx=8, pady=(6, 2))

        self._bar = self._canvas.create_rectangle(0, 0, 0, height, fill=bar_color, width=0)

        # Draw ticks and labels
        scale_dbm = [-121, -115, -109, -103, -97, -91, -85, -79, -73,
                     -67, -61, -55, -49, -43, -37, -33]
        for dbm in scale_dbm:
            x = self.dbm_to_meter(dbm) * width
            self._canvas.create_line(x, 10, x, height, fill=tick_color)

        labels = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9",
                  "+10", "+20", "+30", "+40", "+50", "+60"]
        for i, label in enumerate(labels):
            dbm = scale_dbm[i + 1]
            x = self.dbm_to_meter(dbm) * width
            self._canvas.create_text(x, 6, text=label, fill=text_color, font=("Segoe UI", 8))

        # Info frame
        info = tk.Frame(self._root, bg=bg_main)
        info.pack(pady=(2, 4))

        self._dbm_label = tk.Label(info, text="-120 dBm", font=("Segoe UI", 11),
                                   fg=text_color, bg=bg_main)
        self._dbm_label.pack(side="left", padx=12)

        self._s_inst_label = tk.Label(info, text="S0", font=("Segoe UI", 11),
                                      fg=text_color, bg=bg_main)
        self._s_inst_label.pack(side="left", padx=12)

        self._s_avg_label = tk.Label(info, text="S0 avg", font=("Segoe UI", 11),
                                     fg=text_color, bg=bg_main)
        self._s_avg_label.pack(side="left", padx=12)

        # Offset indicator canvas
        self._offset_canvas = tk.Canvas(
            self._root,
            width=200,
            height=30,
            bg=bg_main,
            highlightthickness=0
        )
        self._offset_canvas.pack(pady=(0, 6))

        # Create the three possible bars
        self._tx_bar = self._offset_canvas.create_rectangle(95, 5, 105, 25, fill="#D10000", width=0)
        self._rx_bar = self._offset_canvas.create_rectangle(95, 5, 105, 25, fill="#00D100", width=0)
        self._yellow_bar = self._offset_canvas.create_rectangle(95, 5, 105, 25, fill="#D1A100", width=0)

        # Initial state: combined mode -> yellow bar visible, others hidden
        self._offset_canvas.itemconfig(self._tx_bar, state="hidden")
        self._offset_canvas.itemconfig(self._rx_bar, state="hidden")
        self._offset_canvas.itemconfig(self._yellow_bar, state="normal")

        # Draw center line
        self._offset_canvas.create_line(100, 0, 100, 30, fill="#888", width=1)

        # Small labels
        self._offset_canvas.create_text(30, 15, text="TX", fill="#D10000", font=("Segoe UI", 8))
        self._offset_canvas.create_text(170, 15, text="RX", fill="#00D100", font=("Segoe UI", 8))

        self._update_loop()
        self._root.mainloop()

    # ---------- Split display update ----------
    def _update_split(self, mode: int, rx_freq: int, tx_freq: int) -> None:
        self._split_mode = mode
        self._rx_freq = rx_freq
        self._tx_freq = tx_freq

        # Frequency difference in Hz
        diff_hz = (rx_freq - tx_freq) if (rx_freq is not None and tx_freq is not None) else 0

        # Scale: 1 pixel per 10 Hz, clamped to ±40 pixels (=> ±400 Hz range)
        max_offset = 40
        offset = int(diff_hz / 10)
        offset = max(-max_offset, min(max_offset, offset))

        if mode == 1:  # Combined – single yellow bar
            self._offset_canvas.itemconfig(self._tx_bar, state="hidden")
            self._offset_canvas.itemconfig(self._rx_bar, state="hidden")
            self._offset_canvas.itemconfig(self._yellow_bar, state="normal")
            self._offset_canvas.coords(self._yellow_bar, 95, 5, 105, 25)

        else:
            self._offset_canvas.itemconfig(self._yellow_bar, state="hidden")

            if mode == 2:  # Split RX – green moves, red fixed
                self._offset_canvas.itemconfig(self._tx_bar, fill="#570000", state="normal")
                self._offset_canvas.itemconfig(self._rx_bar, fill="#00D100", state="normal")
                self._offset_canvas.coords(self._tx_bar, 95, 5, 105, 25)
                new_x = 100 + offset
                # Clamp to canvas boundaries
                new_x = max(15, min(185, new_x))
                self._offset_canvas.coords(self._rx_bar, new_x - 5, 5, new_x + 5, 25)

            elif mode == 3:  # Split TX – red moves, green fixed
                self._offset_canvas.itemconfig(self._tx_bar, fill="#D10000", state="normal")
                self._offset_canvas.itemconfig(self._rx_bar, fill="#005700", state="normal")
                self._offset_canvas.coords(self._rx_bar, 95, 5, 105, 25)
                new_x = 100 - offset
                new_x = max(15, min(185, new_x))
                self._offset_canvas.coords(self._tx_bar, new_x - 5, 5, new_x + 5, 25)

            elif mode == 4:  # Split combined – both bright, fixed offset
                self._offset_canvas.itemconfig(self._tx_bar, fill="#D10000", state="normal")
                self._offset_canvas.itemconfig(self._rx_bar, fill="#00D100", state="normal")

                # Desired positions: TX left, RX right when diff > 0
                tx_x = 100 - offset
                rx_x = 100 + offset

                # Prevent crossing: if tx_x would be to the right of rx_x, swap or clamp
                if tx_x > rx_x:
                    # Place them side by side with a 2‑pixel gap around center
                    tx_x = 98
                    rx_x = 102

                # Clamp to canvas edges (keep bars fully visible)
                tx_x = max(10, min(190, tx_x))
                rx_x = max(10, min(190, rx_x))

                # Ensure a minimum gap of 2 pixels between bars
                if rx_x - tx_x < 4:
                    tx_x = (tx_x + rx_x) // 2 - 2
                    rx_x = tx_x + 4

                self._offset_canvas.coords(self._tx_bar, tx_x - 5, 5, tx_x + 5, 25)
                self._offset_canvas.coords(self._rx_bar, rx_x - 5, 5, rx_x + 5, 25)

    def _toggle(self) -> None:
        if not self._root:
            return
        if self._root.state() == "normal":
            self._root.withdraw()
        else:
            self._root.deiconify()

    def _update_loop(self) -> None:
        # Process commands
        try:
            while True:
                cmd = self._command_queue.get_nowait()
                cmd()
        except queue.Empty:
            pass

        # Update S‑meter
        now = time.time()
        try:
            while True:
                t, dbm = self._queue.get_nowait()
                self._samples.append((t, dbm))
        except queue.Empty:
            pass

        self._samples = [(t, v) for (t, v) in self._samples if now - t < self._avg_window]

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

        self._root.after(40, self._update_loop)