"""
ThermoNoOC — Desktop Control Panel v2

ESP32 Access Point
    SSID:     ThermoNoOC
    Password: thermonooc
    IP:       192.168.4.1
    WS URL:   ws://192.168.4.1:5000

Telemetry JSON (1 s cadence):
    temp1, temp2   – temperature °C (SHT35 x2)
    hum1,  hum2    – humidity %    (SHT35 x2)
    uvIndex        – UV index      (LTR390)
    uvW            – irradiance W/m²
    co2            – CO₂ concentration % (0–100 scale, T6615)
    flow1, flow2   – µL/min       (microfluidics)
    fluidTemp1, fluidTemp2 – °C (microfluidics)

Commands sent:
    SET_TEMP:<float>                          – temperature setpoint
    SET_LED:<group>:<0|1>:<0-100>             – UV LED group
    SET_PUMP:<cir>:<flow>:<pulsed>:<feed>:<pause>:<cycles>
    SET_PRIMING:<cir>:<0|1>              – priming mode (1 = 400 Hz + max V; 0 = restore)

Dependencies:
    pip install customtkinter matplotlib websocket-client
"""

from __future__ import annotations

import csv
import datetime
import json
import pathlib
import threading
from collections import deque
from typing import Callable, Optional

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import customtkinter as ctk
import websocket  # websocket-client package

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
WS_URL     = "ws://192.168.4.1:5000"
BUFFER_LEN = 120      # 120 s of history at 1 s/sample
REDRAW_MS  = 1000     # chart refresh cadence (ms)

# Per-series colours – visible on both dark and light backgrounds
CLR = {
    "temp1": "#FF6B6B",
    "temp2": "#FF9F43",
    "hum1":  "#26de81",
    "hum2":  "#4FC3F7",
    "co2":   "#A29BFE",
    "uv":    "#FD79A8",
    "flow1":      "#00CEC9",
    "flow2":      "#6C5CE7",
    "fluidTemp1": "#74B9FF",
    "fluidTemp2": "#FDCB6E",
}

# Chart theme palettes
_DARK  = dict(bg="#1e1e2e", ax_bg="#252535", fg="#d4d4e8", grid="#353560")
_LIGHT = dict(bg="#f0f4f8", ax_bg="#ffffff",  fg="#1a1a2e", grid="#d0d8e8")

# Send button colour states
_SEND_IDLE        = ("gray78", "gray23")
_SEND_IDLE_HOVER  = ("gray68", "gray32")
_SEND_READY       = "#1f6aa5"
_SEND_READY_HOVER = "#1a5a8e"


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket client — runs in a daemon thread; all callbacks fire from that thread
# ─────────────────────────────────────────────────────────────────────────────
class WSClient:
    def __init__(self) -> None:
        self._ws:     Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread]       = None
        self.connected: bool = False
        self.on_data:   Optional[Callable[[dict], None]] = None
        self.on_status: Optional[Callable[[bool], None]] = None

    def connect(self, url: str) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, args=(url,), daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        if self._ws:
            self._ws.close()

    def send(self, msg: str) -> None:
        if self._ws and self.connected:
            try:
                self._ws.send(msg)
            except Exception as e:
                print(f"[WSClient] send failed for {msg!r}: {e}")
        else:
            print(f"[WSClient] dropped {msg!r} — not connected")

    # private ──────────────────────────────────────────────────────────────────
    def _run(self, url: str) -> None:
        self._ws = websocket.WebSocketApp(
            url,
            on_open    = self._cb_open,
            on_message = self._cb_message,
            on_error   = self._cb_error,
            on_close   = self._cb_close,
        )
        self._ws.run_forever(ping_interval=10, ping_timeout=5)

    def _cb_open(self, ws):
        self.connected = True
        if self.on_status:
            self.on_status(True)

    def _cb_message(self, ws, raw: str):
        try:
            if self.on_data:
                self.on_data(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            pass

    def _cb_error(self, ws, err):
        pass

    def _cb_close(self, ws, code, msg):
        self.connected = False
        if self.on_status:
            self.on_status(False)

# ─────────────────────────────────────────────────────────────────────────────
# Chart utilities
# ─────────────────────────────────────────────────────────────────────────────
def _pal() -> dict:
    return _DARK if ctk.get_appearance_mode().lower() == "dark" else _LIGHT


def _style_ax(ax, pal: dict, ylabel: str = "") -> None:
    ax.set_facecolor(pal["ax_bg"])
    ax.tick_params(colors=pal["fg"], labelsize=8)
    ax.set_ylabel(ylabel, color=pal["fg"], fontsize=9)
    ax.set_xlabel("Time (s)", color=pal["fg"], fontsize=8)
    for sp in ax.spines.values():
        sp.set_edgecolor(pal["grid"])
    ax.grid(True, color=pal["grid"], linewidth=0.4, linestyle="--", alpha=0.7)


def _make_fig(height: float = 2.8) -> tuple:
    pal = _pal()
    fig, ax = plt.subplots(figsize=(9, height), dpi=96)
    fig.patch.set_facecolor(pal["bg"])
    fig.subplots_adjust(left=0.08, right=0.97, top=0.94, bottom=0.16)
    _style_ax(ax, pal)
    return fig, ax


def _draw_series(ax, t: list, series: list, pal: dict, ylabel: str = "") -> None:
    ax.cla()
    _style_ax(ax, pal, ylabel)
    for y, label, color in series:
        ax.plot(t, y, color=color, linewidth=1.6, label=label, solid_capstyle="round")
    ax.set_ylim(bottom=0)
    if len(series) > 1:
        ax.legend(fontsize=8, framealpha=0.25,
                  facecolor=pal["ax_bg"], labelcolor=pal["fg"],
                  edgecolor=pal["grid"])


# ─────────────────────────────────────────────────────────────────────────────
# Main application
# ─────────────────────────────────────────────────────────────────────────────
class App(ctk.CTk):
    # ── init ──────────────────────────────────────────────────────────────────
    def __init__(self) -> None:
        super().__init__()
        self.title("ThermoNoOC — Control Panel")
        self.geometry("1140x760")
        self.minsize(860, 580)

        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        # data buffers
        self._lock    = threading.Lock()
        self._elapsed = 0.0
        self._t: deque = deque(
            [round(-1.0 * (BUFFER_LEN - 1 - i), 1) for i in range(BUFFER_LEN)],
            maxlen=BUFFER_LEN,
        )
        self._bufs: dict[str, deque] = {
            k: deque([0.0] * BUFFER_LEN, maxlen=BUFFER_LEN)
            for k in ("temp1", "temp2", "hum1", "hum2", "co2", "uv_irr", "uv_idx", "flow1", "flow2", "fluidTemp1", "fluidTemp2")
        }

        # websocket
        self._ws           = WSClient()
        self._ws.on_data   = self._ingest
        self._ws.on_status = lambda ok: self.after(0, self._set_connected, ok)
        self._was_connecting = False

        # pending changes flag – controls Send Data button appearance
        self._pending_changes = False
        self._micro_closed    = False   # pre-init; overwritten by _build_micro_sheet

        # last values confirmed sent – None means never sent
        self._last_sent_temp: Optional[float]       = None
        self._last_sent_led:  list[Optional[tuple]] = [None] * 4
        self._last_sent_pump: list[Optional[tuple]] = [None, None]

        # CSV logging
        self._csv_lock   = threading.Lock()
        self._csv_file   = None
        self._csv_writer = None

        # build
        self._active  = 0
        self._closing = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(REDRAW_MS, self._tick)
        self._update_clock()

    # ── layout skeleton ───────────────────────────────────────────────────────
    def _build(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._build_header()
        self._build_content()

    # ── header bar ────────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, height=56, corner_radius=0,
                           fg_color=("gray88", "gray13"))
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        # elastic spacer pushes controls to the right
        hdr.grid_columnconfigure(4, weight=1)

        TAB_NAMES = ["  Incubator  ", "  UV Light  ", "  Microfluidics  "]
        self._tab_btns: list[ctk.CTkButton] = []
        for i, name in enumerate(TAB_NAMES):
            b = ctk.CTkButton(
                hdr, text=name, width=148, height=36, corner_radius=6,
                fg_color=("gray78", "gray23"),
                hover_color=("gray68", "gray32"),
                text_color=("black", "white"),
                command=lambda i=i: self._show(i),
            )
            b.grid(row=0, column=i, padx=(10 if i == 0 else 3, 0), pady=10)
            self._tab_btns.append(b)

        # clock — left of connect button
        self._clock_lbl = ctk.CTkLabel(hdr, text="00:00:00",
                                       font=ctk.CTkFont(family="Helvetica", size=20, weight="bold"))
        self._clock_lbl.grid(row=0, column=5, padx=(0, 12))
        
        # status indicator
        self._dot = ctk.CTkLabel(hdr, text="●",
                                 font=ctk.CTkFont(size=17), text_color="gray42")
        self._dot.grid(row=0, column=6, padx=(14, 2))

        self._conn_lbl = ctk.CTkLabel(hdr, text="Disconnected",
                                      font=ctk.CTkFont(size=11))
        self._conn_lbl.grid(row=0, column=7, padx=(0, 12))

        # connect / disconnect
        self._conn_btn = ctk.CTkButton(
            hdr, text="Connect", width=100, height=34, corner_radius=6,
            command=self._toggle_connect,
        )
        self._conn_btn.grid(row=0, column=8, padx=4)

        # send data — disabled until the active subsystem is confirmed closed
        self._send_btn = ctk.CTkButton(
            hdr, text="Send Data", width=100, height=34, corner_radius=6,
            fg_color=_SEND_IDLE, hover_color=_SEND_IDLE_HOVER,
            state="disabled",
            command=self._send_data,
        )
        self._send_btn.grid(row=0, column=9, padx=4)

        # theme toggle
        self._theme_btn = ctk.CTkButton(
            hdr, text="☀", width=40, height=34, corner_radius=6,
            fg_color=("gray78", "gray23"), hover_color=("gray68", "gray32"),
            font=ctk.CTkFont(size=15),
            command=self._toggle_theme,
        )
        self._theme_btn.grid(row=0, column=10, padx=(4, 12))

        self._highlight_tab()

    # ── scrollable content area ───────────────────────────────────────────────
    def _build_content(self) -> None:
        self._scroll = ctk.CTkScrollableFrame(
            self, corner_radius=0, fg_color="transparent"
        )
        self._scroll.grid(row=1, column=0, sticky="nsew")
        self._scroll.grid_columnconfigure(0, weight=1)

        self._sheets = [
            self._build_incubator_sheet(),
            self._build_uv_sheet(),
            self._build_micro_sheet(),
        ]
        self._show(0)

    # ── Sheet 1: Incubator ────────────────────────────────────────────────────
    def _build_incubator_sheet(self) -> ctk.CTkFrame:
        fr = ctk.CTkFrame(self._scroll, fg_color="transparent")
        fr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(fr, text="Incubator Monitoring",
                     font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(18, 6), sticky="w")

        # Safety toggle — must be Closed before sensors/actuators run
        self._incubator_closed = False
        self._incubator_want = False
        self._incubator_btn = ctk.CTkButton(
            fr,
            text="Incubator Opened",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#c0392b",
            hover_color="#a93226",
            text_color="white",
            width=220, height=40,
            corner_radius=8,
            command=self._toggle_incubator,
        )
        self._incubator_btn.grid(row=1, column=0, padx=20, pady=(0, 14), sticky="w")

        # Temperature row — plot left, sensor values info box right
        temp_row = ctk.CTkFrame(fr, fg_color="transparent")
        temp_row.grid(row=2, column=0, padx=20, pady=(0, 10), sticky="ew")
        temp_row.grid_columnconfigure(0, weight=1)
        temp_row.grid_columnconfigure(1, weight=0)

        temp_plot = ctk.CTkFrame(temp_row, corner_radius=12)
        temp_plot.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkLabel(temp_plot, text="Temperature (°C)",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4))
        self._temp_fig, self._temp_ax = _make_fig(2.8)
        self._temp_canvas = FigureCanvasTkAgg(self._temp_fig, master=temp_plot)
        self._temp_canvas.draw()
        self._temp_canvas.get_tk_widget().pack(fill="x", padx=8, pady=(0, 6))

        # Target temperature slider below the plot
        ctrl_row = ctk.CTkFrame(temp_plot, fg_color="transparent")
        ctrl_row.pack(fill="x", padx=8, pady=(0, 10))
        ctk.CTkLabel(ctrl_row, text="Target Temperature:",
                     font=ctk.CTkFont(size=12), width=52, anchor="w").pack(side="left")
        self._temp_set_var = ctk.DoubleVar(value=20.0)
        self._temp_set_lbl = ctk.CTkLabel(ctrl_row, text="20.0 °C", width=62, anchor="e",
                                           font=ctk.CTkFont(size=11))
        self._temp_set_lbl.pack(side="right", padx=(0, 4))
        ctk.CTkSlider(
            ctrl_row, from_=20, to=60,
            variable=self._temp_set_var,
            command=lambda _: self._on_ctrl_change(),
        ).pack(side="left", fill="x", expand=True, padx=(4, 4))
        self._temp_set_var.trace_add(
            "write",
            lambda *_: self._temp_set_lbl.configure(
                text=f"{self._temp_set_var.get():.1f} °C"
            ),
        )

        info_temp = ctk.CTkFrame(temp_row, width=196, corner_radius=12)
        info_temp.grid(row=0, column=1, sticky="ns")
        info_temp.pack_propagate(False)

        ctk.CTkLabel(info_temp, text="Top Sensor",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(20, 2))
        self._temp1_val = ctk.CTkLabel(
            info_temp, text="—",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color=CLR["temp1"],
        )
        self._temp1_val.pack()
        ctk.CTkLabel(info_temp, text="°C", font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 10))

        ctk.CTkFrame(info_temp, height=1, fg_color=("gray72", "gray28")).pack(fill="x", padx=14)

        ctk.CTkLabel(info_temp, text="Bottom Sensor",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(10, 2))
        self._temp2_val = ctk.CTkLabel(
            info_temp, text="—",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color=CLR["temp2"],
        )
        self._temp2_val.pack()
        ctk.CTkLabel(info_temp, text="°C", font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 20))

        # Humidity row — plot left, sensor values info box right
        hum_row = ctk.CTkFrame(fr, fg_color="transparent")
        hum_row.grid(row=3, column=0, padx=20, pady=(0, 10), sticky="ew")
        hum_row.grid_columnconfigure(0, weight=1)
        hum_row.grid_columnconfigure(1, weight=0)

        hum_plot = ctk.CTkFrame(hum_row, corner_radius=12)
        hum_plot.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkLabel(hum_plot, text="Humidity (%)",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4))
        self._hum_fig, self._hum_ax = _make_fig(2.8)
        self._hum_canvas = FigureCanvasTkAgg(self._hum_fig, master=hum_plot)
        self._hum_canvas.draw()
        self._hum_canvas.get_tk_widget().pack(fill="x", padx=8, pady=(0, 10))

        info_hum = ctk.CTkFrame(hum_row, width=196, corner_radius=12)
        info_hum.grid(row=0, column=1, sticky="ns")
        info_hum.pack_propagate(False)

        ctk.CTkLabel(info_hum, text="Top Sensor",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(20, 2))
        self._hum1_val = ctk.CTkLabel(
            info_hum, text="—",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color=CLR["hum1"],
        )
        self._hum1_val.pack()
        ctk.CTkLabel(info_hum, text="%", font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 10))

        ctk.CTkFrame(info_hum, height=1, fg_color=("gray72", "gray28")).pack(fill="x", padx=14)

        ctk.CTkLabel(info_hum, text="Bottom Sensor",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(10, 2))
        self._hum2_val = ctk.CTkLabel(
            info_hum, text="—",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color=CLR["hum2"],
        )
        self._hum2_val.pack()
        ctk.CTkLabel(info_hum, text="%", font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 20))

        # CO₂ row — plot left, PPM info box right
        co2_row = ctk.CTkFrame(fr, fg_color="transparent")
        co2_row.grid(row=4, column=0, padx=20, pady=(0, 10), sticky="ew")
        co2_row.grid_columnconfigure(0, weight=1)
        co2_row.grid_columnconfigure(1, weight=0)

        co2_plot = ctk.CTkFrame(co2_row, corner_radius=12)
        co2_plot.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkLabel(co2_plot, text="CO₂ Concentration (%)",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4))
        self._co2_fig, self._co2_ax = _make_fig(2.8)
        self._co2_canvas = FigureCanvasTkAgg(self._co2_fig, master=co2_plot)
        self._co2_canvas.draw()
        self._co2_canvas.get_tk_widget().pack(fill="x", padx=8, pady=(0, 10))

        info_co2 = ctk.CTkFrame(co2_row, width=196, corner_radius=12)
        info_co2.grid(row=0, column=1, sticky="ns")
        info_co2.pack_propagate(False)

        ctk.CTkLabel(info_co2, text="CO₂",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(28, 4))
        self._co2_ppm_val = ctk.CTkLabel(
            info_co2, text="—",
            font=ctk.CTkFont(size=50, weight="bold"),
            text_color=CLR["co2"],
        )
        self._co2_ppm_val.pack()
        ctk.CTkLabel(info_co2, text="ppm",
                     font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 24))

        return fr

    # ── Sheet 2: UV Light ─────────────────────────────────────────────────────
    def _build_uv_sheet(self) -> ctk.CTkFrame:
        fr = ctk.CTkFrame(self._scroll, fg_color="transparent")
        fr.grid_columnconfigure(0, weight=1)
        fr.grid_columnconfigure(1, weight=0)

        ctk.CTkLabel(fr, text="UV Light",
                     font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, columnspan=2, padx=20, pady=(18, 6), sticky="w")

        # Irradiance plot + LED controls (left, expands)
        plot_card = ctk.CTkFrame(fr, corner_radius=12)
        plot_card.grid(row=1, column=0, padx=(20, 6), pady=(0, 24), sticky="nsew")
        ctk.CTkLabel(plot_card, text="Irradiance (W/m²)",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4))
        self._uv_fig, self._uv_ax = _make_fig(3.6)
        self._uv_canvas = FigureCanvasTkAgg(self._uv_fig, master=plot_card)
        self._uv_canvas.draw()
        self._uv_canvas.get_tk_widget().pack(fill="x", padx=8, pady=(0, 10))

        # UV LED group controls below the irradiance plot — 2×2 grid of cards
        ctk.CTkLabel(plot_card, text="UV LED Groups",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(4, 4))

        led_grid = ctk.CTkFrame(plot_card, fg_color="transparent")
        led_grid.pack(fill="x", padx=8, pady=(0, 10))
        led_grid.grid_columnconfigure(0, weight=1)
        led_grid.grid_columnconfigure(1, weight=1)

        self._led_en:  list[ctk.BooleanVar] = []
        self._led_int: list[ctk.IntVar]     = []

        for i in range(4):
            r, c = divmod(i, 2)
            card = ctk.CTkFrame(led_grid, corner_radius=8)
            card.grid(row=r, column=c, padx=4, pady=4, sticky="nsew")

            ctk.CTkLabel(card, text=f"LED Group {i + 1}",
                         font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(10, 6))

            var_en = ctk.BooleanVar(value=False)
            var_int = ctk.IntVar(value=50)

            pct = ctk.CTkLabel(card, text="50%", font=ctk.CTkFont(size=10))
            slider = ctk.CTkSlider(
                card, from_=0, to=100,
                variable=var_int, height=12, button_length=12,
                state="disabled",
                command=lambda _: self._on_ctrl_change(),
            )

            def _en_cmd(sw=var_en, sl=slider):
                sl.configure(state="normal" if sw.get() else "disabled")
                self._on_ctrl_change()

            en_row = ctk.CTkFrame(card, fg_color="transparent")
            en_row.pack(pady=(0, 6))
            ctk.CTkLabel(en_row, text="Enable",
                         font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 6))
            ctk.CTkSwitch(en_row, variable=var_en, text="",
                          width=44, height=22,
                          command=_en_cmd).pack(side="left")
            self._led_en.append(var_en)

            pct.pack()
            slider.pack(fill="x", padx=14, pady=(2, 12))
            self._led_int.append(var_int)
            var_int.trace_add(
                "write",
                lambda *_, v=var_int, lbl=pct: lbl.configure(text=f"{v.get()}%"),
            )

        # Info box (right, fixed width)
        info = ctk.CTkFrame(fr, width=196, corner_radius=12)
        info.grid(row=1, column=1, padx=(0, 20), pady=(0, 24), sticky="ns")
        info.pack_propagate(False)

        ctk.CTkLabel(info, text="UVI Index",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(28, 4))
        self._uvi_val = ctk.CTkLabel(
            info, text="—",
            font=ctk.CTkFont(size=50, weight="bold"),
            text_color=CLR["uv"],
        )
        self._uvi_val.pack()
        ctk.CTkLabel(info, text="unitless",
                     font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 24))

        ctk.CTkFrame(info, height=1, fg_color=("gray72", "gray28")).pack(fill="x", padx=14)

        ctk.CTkLabel(info, text="Irradiance",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(22, 4))
        self._irr_val = ctk.CTkLabel(
            info, text="—",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=("gray25", "gray85"),
        )
        self._irr_val.pack()
        ctk.CTkLabel(info, text="W/m²",
                     font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 24))

        return fr

    # ── Sheet 3: Microfluidics ────────────────────────────────────────────────
    def _build_micro_sheet(self) -> ctk.CTkFrame:
        fr = ctk.CTkFrame(self._scroll, fg_color="transparent")
        fr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(fr, text="Microfluidics Control",
                     font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(18, 6), sticky="w")

        # ── Status button ─────────────────────────────────────────────────────
        btn_row = ctk.CTkFrame(fr, fg_color="transparent")
        btn_row.grid(row=1, column=0, padx=20, pady=(0, 14), sticky="w")

        self._micro_closed = False
        self._micro_want = False
        self._micro_btn = ctk.CTkButton(
            btn_row,
            text="Microfluidics Opened",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#c0392b", hover_color="#a93226",
            text_color="white", width=220, height=40, corner_radius=8,
            command=self._toggle_micro,
        )
        self._micro_btn.grid(row=0, column=0)

        # ── Flow sensors row ──────────────────────────────────────────────────
        flow_row = ctk.CTkFrame(fr, fg_color="transparent")
        flow_row.grid(row=2, column=0, padx=20, pady=(0, 10), sticky="ew")
        flow_row.grid_columnconfigure(0, weight=1)
        flow_row.grid_columnconfigure(1, weight=0)
        flow_row.grid_columnconfigure(2, weight=0)

        flow_plot = ctk.CTkFrame(flow_row, corner_radius=12)
        flow_plot.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkLabel(flow_plot, text="Flow Rate (µL/min)",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4))
        self._flow_fig, self._flow_ax = _make_fig(2.8)
        self._flow_canvas = FigureCanvasTkAgg(self._flow_fig, master=flow_plot)
        self._flow_canvas.draw()
        self._flow_canvas.get_tk_widget().pack(fill="x", padx=8, pady=(0, 10))

        info_flow = ctk.CTkFrame(flow_row, width=196, corner_radius=12)
        info_flow.grid(row=0, column=1, sticky="ns")
        info_flow.pack_propagate(False)

        ctk.CTkLabel(info_flow, text="Flow Circuit 1",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(20, 2))
        self._flow1_val = ctk.CTkLabel(
            info_flow, text="—",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color=CLR["flow1"],
        )
        self._flow1_val.pack()
        ctk.CTkLabel(info_flow, text="µL/min", font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 10))

        ctk.CTkFrame(info_flow, height=1, fg_color=("gray72", "gray28")).pack(fill="x", padx=14)

        ctk.CTkLabel(info_flow, text="Flow Circuit 2",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(10, 2))
        self._flow2_val = ctk.CTkLabel(
            info_flow, text="—",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color=CLR["flow2"],
        )
        self._flow2_val.pack()
        ctk.CTkLabel(info_flow, text="µL/min", font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 20))

        info_temp_fluid = ctk.CTkFrame(flow_row, width=196, corner_radius=12)
        info_temp_fluid.grid(row=0, column=2, padx=(6, 0), sticky="ns")
        info_temp_fluid.pack_propagate(False)

        ctk.CTkLabel(info_temp_fluid, text="Temp Circuit 1",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(20, 2))
        self._fluidTemp1_val = ctk.CTkLabel(
            info_temp_fluid, text="—",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color=CLR["fluidTemp1"],
        )
        self._fluidTemp1_val.pack()
        ctk.CTkLabel(info_temp_fluid, text="°C", font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 10))

        ctk.CTkFrame(info_temp_fluid, height=1, fg_color=("gray72", "gray28")).pack(fill="x", padx=14)

        ctk.CTkLabel(info_temp_fluid, text="Temp Circuit 2",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(10, 2))
        self._fluidTemp2_val = ctk.CTkLabel(
            info_temp_fluid, text="—",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color=CLR["fluidTemp2"],
        )
        self._fluidTemp2_val.pack()
        ctk.CTkLabel(info_temp_fluid, text="°C", font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray55")).pack(pady=(2, 20))

        # ── Pump configuration ────────────────────────────────────────────────
        pumps_row = ctk.CTkFrame(fr, fg_color="transparent")
        pumps_row.grid(row=3, column=0, padx=20, pady=(0, 20), sticky="ew")
        pumps_row.grid_columnconfigure(0, weight=1)
        pumps_row.grid_columnconfigure(1, weight=1)

        self._pump_mode_vars  = []
        self._pump_frames     = []
        self._pump_vars       = []   # [{flow, volume, feed, pause}, ...]
        self._priming_active  = [False, False]
        self._priming_btns: list[ctk.CTkButton] = []

        for i in range(2):
            pump_card = ctk.CTkFrame(pumps_row, corner_radius=12)
            pump_card.grid(row=0, column=i, padx=4, pady=4, sticky="nsew")

            ctk.CTkLabel(pump_card, text=f"Pump {i + 1}",
                         font=ctk.CTkFont(size=14, weight="bold")).pack(
                anchor="w", padx=14, pady=(12, 8))

            mode_var = ctk.StringVar(value="Continuous")
            self._pump_mode_vars.append(mode_var)

            ctk.CTkSegmentedButton(
                pump_card,
                values=["Continuous", "Pulsed"],
                variable=mode_var,
                command=lambda val, idx=i: self._on_pump_mode_change(idx, val),
            ).pack(fill="x", padx=14, pady=(0, 10))

            # Numeric-only StringVars for every entry field in this pump
            sv_flow        = self._num_var()
            sv_volume      = self._num_var()
            sv_feed        = self._num_var()
            sv_pause       = self._num_var()
            sv_cycles_n    = self._num_var()
            var_cycles_inf = ctk.BooleanVar(value=True)
            self._pump_vars.append({
                "flow": sv_flow, "volume": sv_volume,
                "feed": sv_feed, "pause": sv_pause,
                "cycles_inf": var_cycles_inf, "cycles_n": sv_cycles_n,
            })

            # Continuous frame
            cont_frame = ctk.CTkFrame(pump_card, fg_color="transparent")
            cont_inner = ctk.CTkFrame(cont_frame, fg_color="transparent")
            cont_inner.pack(fill="x", padx=14, pady=(0, 14))
            ctk.CTkLabel(cont_inner, text="Flow Rate:",
                         font=ctk.CTkFont(size=12), width=90, anchor="w").pack(side="left")
            ctk.CTkEntry(cont_inner, width=90, placeholder_text="0 – 2000",
                         textvariable=sv_flow).pack(side="left", padx=(4, 4))
            ctk.CTkLabel(cont_inner, text="µL/min",
                         font=ctk.CTkFont(size=11),
                         text_color=("gray55", "gray55")).pack(side="left")

            # Pulsed frame
            pulsed_frame = ctk.CTkFrame(pump_card, fg_color="transparent")
            for lbl_txt, ph_txt, unit_txt, sv in [
                ("Volume:",           "µL", "µL", sv_volume),
                ("Feeding time:",     "s",  "s",  sv_feed),
                ("Non-feeding time:", "s",  "s",  sv_pause),
            ]:
                pf_row = ctk.CTkFrame(pulsed_frame, fg_color="transparent")
                pf_row.pack(fill="x", padx=14, pady=(0, 6))
                ctk.CTkLabel(pf_row, text=lbl_txt,
                             font=ctk.CTkFont(size=12), width=138, anchor="w").pack(side="left")
                ctk.CTkEntry(pf_row, width=80, placeholder_text=ph_txt,
                             textvariable=sv).pack(side="left", padx=(4, 4))
                ctk.CTkLabel(pf_row, text=unit_txt,
                             font=ctk.CTkFont(size=11),
                             text_color=("gray55", "gray55")).pack(side="left")
            # Cycles row — infinite toggle + optional count entry
            cyc_row = ctk.CTkFrame(pulsed_frame, fg_color="transparent")
            cyc_row.pack(fill="x", padx=14, pady=(0, 6))
            ctk.CTkLabel(cyc_row, text="Cycles:",
                         font=ctk.CTkFont(size=12), width=138, anchor="w").pack(side="left")
            cyc_n_entry = ctk.CTkEntry(cyc_row, width=80, placeholder_text="1",
                                       textvariable=sv_cycles_n, state="disabled")

            def _cyc_toggle(inf=var_cycles_inf, entry=cyc_n_entry):
                entry.configure(state="disabled" if inf.get() else "normal")
                self._on_ctrl_change()

            ctk.CTkSwitch(cyc_row, variable=var_cycles_inf, text="Infinite",
                          font=ctk.CTkFont(size=11), width=44, height=22,
                          command=_cyc_toggle).pack(side="left", padx=(0, 8))
            cyc_n_entry.pack(side="left", padx=(4, 4))
            ctk.CTkLabel(cyc_row, text="cycles",
                         font=ctk.CTkFont(size=11),
                         text_color=("gray55", "gray55")).pack(side="left")
            ctk.CTkFrame(pulsed_frame, height=8, fg_color="transparent").pack()

            self._pump_frames.append((cont_frame, pulsed_frame))
            cont_frame.pack(fill="x")  # continuous shown by default

            # ── Priming button ────────────────────────────────────────────────
            ctk.CTkFrame(pump_card, height=1,
                         fg_color=("gray72", "gray28")).pack(fill="x", padx=14, pady=(10, 0))

            priming_btn = ctk.CTkButton(
                pump_card,
                text="Priming  OFF",
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color=("gray78", "gray23"),
                hover_color=("gray68", "gray32"),
                text_color=("black", "white"),
                width=180, height=36, corner_radius=8,
                command=lambda idx=i: self._toggle_priming(idx),
            )
            priming_btn.pack(pady=(8, 2))
            self._priming_btns.append(priming_btn)

            ctk.CTkLabel(
                pump_card,
                text="Under development, do not use",
                font=ctk.CTkFont(size=10),
                text_color=("gray55", "gray55"),
            ).pack(pady=(0, 12))

        return fr

    # ── card factory ──────────────────────────────────────────────────────────
    @staticmethod
    def _card(parent: ctk.CTkFrame, row: int, title: str = "") -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.grid(row=row, column=0, padx=20, pady=(0, 10), sticky="ew")
        if title:
            ctk.CTkLabel(card, text=title,
                         font=ctk.CTkFont(size=12, weight="bold")).pack(
                anchor="w", padx=12, pady=(10, 4))
        return card

    # ── tab navigation ────────────────────────────────────────────────────────
    def _show(self, index: int) -> None:
        for i, sh in enumerate(self._sheets):
            if i == index:
                sh.grid(row=0, column=0, sticky="nsew")
            else:
                sh.grid_remove()
        self._active = index
        self._highlight_tab()
        self._update_send_btn()

    def _highlight_tab(self) -> None:
        for i, b in enumerate(self._tab_btns):
            if i == self._active:
                b.configure(fg_color="#1f6aa5", text_color="white")
            else:
                b.configure(fg_color=("gray78", "gray23"), text_color=("black", "white"))

    # ── incubator safety toggle ───────────────────────────────────────────────
    def _toggle_incubator(self) -> None:
        self._incubator_want = not self._incubator_want
        self._incubator_closed = self._incubator_want
        if self._incubator_closed:
            btn_kw = dict(text="Incubator Closed", fg_color="#1e8449", hover_color="#196f3d")
        else:
            btn_kw = dict(text="Incubator Opened", fg_color="#c0392b", hover_color="#a93226")
        self._incubator_btn.configure(**btn_kw)
        self._update_send_btn()
        self._ws.send(f"SET_INCUBATOR:{1 if self._incubator_want else 0}")

    def _sync_incubator_btn(self) -> None:
        if self._incubator_closed:
            self._incubator_btn.configure(text="Incubator Closed", fg_color="#1e8449", hover_color="#196f3d")
        else:
            self._incubator_btn.configure(text="Incubator Opened", fg_color="#c0392b", hover_color="#a93226")
        self._update_send_btn()

    def _sync_micro_btn(self) -> None:
        if self._micro_closed:
            self._micro_btn.configure(text="Microfluidics Closed", fg_color="#1e8449", hover_color="#196f3d")
        else:
            self._micro_btn.configure(text="Microfluidics Opened", fg_color="#c0392b", hover_color="#a93226")
        self._update_send_btn()

    # ── CSV logging ───────────────────────────────────────────────────────────
    def _open_csv(self) -> None:
        now = datetime.datetime.now()
        base = pathlib.Path(__file__).parent / "Data_Logging"
        day_folder = base / now.strftime("%Y%m%d")
        day_folder.mkdir(parents=True, exist_ok=True)
        path = day_folder / f"{now.strftime('%Y%m%d_%H%M%S')}.csv"
        with self._csv_lock:
            self._csv_file = open(path, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow([
                "timestamp",
                "temp1_C", "temp2_C", "hum1_pct", "hum2_pct",
                "co2_pct", "uvW_Wm2", "uvIndex",
                "target_temp_C",
                "led1_en", "led1_int_pct",
                "led2_en", "led2_int_pct",
                "led3_en", "led3_int_pct",
                "led4_en", "led4_int_pct",
            ])
            self._csv_file.flush()

    def _close_csv(self) -> None:
        with self._csv_lock:
            if self._csv_file:
                self._csv_file.close()
                self._csv_file   = None
                self._csv_writer = None

    # ── window close ──────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        self._closing = True
        self._ws.disconnect()
        self._close_csv()
        self.destroy()

    # ── connection ────────────────────────────────────────────────────────────
    def _toggle_connect(self) -> None:
        if self._ws.connected:
            self._ws.disconnect()
        else:
            self._was_connecting = True
            self._dot.configure(text_color="#FFD93D")
            self._conn_lbl.configure(text="Connecting…")
            self._conn_btn.configure(text="Cancel", state="normal")
            self._ws.connect(WS_URL)

    def _set_connected(self, ok: bool) -> None:
        if self._closing or not self.winfo_exists():
            return
        if ok:
            self._was_connecting = False
            self._dot.configure(text_color="#26de81")
            self._conn_lbl.configure(text="Connected")
            self._conn_btn.configure(text="Disconnect")
            self._open_csv()
            # Re-assert current desired state on every (re)connect — covers the
            # case where a toggle was clicked before the socket was fully open,
            # or a ping-timeout silently dropped and reopened the connection.
            self._ws.send(f"SET_INCUBATOR:{1 if self._incubator_closed else 0}")
            self._ws.send(f"SET_MICRO:{1 if self._micro_closed else 0}")
            self._ws.send(f"SET_TEMP:{self._temp_set_var.get():.1f}")
        else:
            if self._was_connecting:
                self._dot.configure(text_color="#FF6B6B")
                self._conn_lbl.configure(text="Connection failed")
            else:
                self._dot.configure(text_color="gray42")
                self._conn_lbl.configure(text="Disconnected")
            self._was_connecting = False
            self._conn_btn.configure(text="Connect")
            self._close_csv()

    # ── clock ─────────────────────────────────────────────────────────────────
    def _update_clock(self) -> None:
        if self._closing or not self.winfo_exists():
            return
        self._clock_lbl.configure(text=datetime.datetime.now().strftime("%H:%M:%S"))
        self.after(1000, self._update_clock)

    # ── control-change tracking ───────────────────────────────────────────────
    def _on_ctrl_change(self, *_) -> None:
        self._pending_changes = True
        self._update_send_btn()

    # ── send data directly ────────────────────────────────────────────────────
    def _send_data(self) -> None:
        if not self._ws.connected:
            return
        if self._active == 2:
            self._send_pump_data()
        else:
            self._send_incubator_data()
        self._pending_changes = False
        self._update_send_btn()

    def _send_incubator_data(self) -> None:
        cur_temp = round(self._temp_set_var.get(), 1)
        if cur_temp != self._last_sent_temp:
            self._ws.send(f"SET_TEMP:{cur_temp:.1f}")
            self._last_sent_temp = cur_temp
        for i, (en, inten) in enumerate(zip(self._led_en, self._led_int)):
            state = (en.get(), inten.get())
            if state != self._last_sent_led[i]:
                self._ws.send(f"SET_LED:{i + 1}:{int(state[0])}:{state[1]}")
                self._last_sent_led[i] = state

    def _send_pump_data(self) -> None:
        for i, pvars in enumerate(self._pump_vars):
            is_pulsed = 1 if self._pump_mode_vars[i].get() == "Pulsed" else 0
            if is_pulsed:
                volume_ul = float(pvars["volume"].get() or "0")
                feed_s    = float(pvars["feed"].get()   or "0")
                pause_s   = float(pvars["pause"].get()  or "0")
                # Firmware expects flow rate (µL/min); derive it from volume ÷ feed time
                flow_rate = (volume_ul / feed_s * 60.0) if feed_s > 0.0 else 0.0
                flow  = f"{flow_rate:.2f}"
                feed  = f"{feed_s:.2f}"
                pause = f"{pause_s:.2f}"
                cycles = 0 if pvars["cycles_inf"].get() else int(pvars["cycles_n"].get() or "1")
            else:
                flow   = pvars["flow"].get() or "0"
                feed   = "0"
                pause  = "0"
                cycles = 0
            state = (is_pulsed, flow, feed, pause, cycles)
            if state != self._last_sent_pump[i]:
                self._ws.send(f"SET_PUMP:{i + 1}:{flow}:{is_pulsed}:{feed}:{pause}:{cycles}")
                self._last_sent_pump[i] = state

    # ── data ingestion (called from WS thread) ────────────────────────────────
    def _ingest(self, data: dict) -> None:
        with self._lock:
            # Resync local interlock state from what the board actually reports —
            # if a SET_INCUBATOR/SET_MICRO command never reached it (e.g. sent before
            # the socket finished connecting), this brings the UI back in line instead
            # of silently logging/displaying data the board never actually collected.
            fw_inc = bool(data.get("incClosed", self._incubator_closed))
            if fw_inc != self._incubator_closed:
                self._incubator_closed = fw_inc
                self.after(0, self._sync_incubator_btn)

            fw_micro = bool(data.get("microClosed", self._micro_closed))
            if fw_micro != self._micro_closed:
                self._micro_closed = fw_micro
                self.after(0, self._sync_micro_btn)

            self._elapsed += 1.0
            self._t.append(self._elapsed)
            self._bufs["temp1"].append(float(data.get("temp1", 0)))
            self._bufs["temp2"].append(float(data.get("temp2", 0)))
            self._bufs["hum1"].append( float(data.get("hum1",  0)))
            self._bufs["hum2"].append( float(data.get("hum2",  0)))
            self._bufs["co2"].append(  float(data.get("co2",   0)))
            self._bufs["uv_irr"].append(float(data.get("uvW",     0)))
            self._bufs["uv_idx"].append(float(data.get("uvIndex", 0)))
            self._bufs["flow1"].append(float(data.get("flow1", 0)))
            self._bufs["flow2"].append(float(data.get("flow2", 0)))
            self._bufs["fluidTemp1"].append(float(data.get("fluidTemp1", 0)))
            self._bufs["fluidTemp2"].append(float(data.get("fluidTemp2", 0)))

        self.after(0, self._redraw)

        with self._csv_lock:
            if self._csv_writer and self._incubator_closed:
                row = [
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    data.get("temp1", ""), data.get("temp2", ""),
                    data.get("hum1",  ""), data.get("hum2",  ""),
                    data.get("co2",   ""), data.get("uvW",   ""), data.get("uvIndex", ""),
                    f"{self._temp_set_var.get():.1f}",
                ]
                for en, inten in zip(self._led_en, self._led_int):
                    row += [int(en.get()), inten.get()]
                self._csv_writer.writerow(row)
                self._csv_file.flush()

    # ── redraw loop ───────────────────────────────────────────────────────────
    def _tick(self) -> None:
        if self._closing or not self.winfo_exists():
            return
        if self._incubator_want != self._incubator_closed:
            self._ws.send(f"SET_INCUBATOR:{1 if self._incubator_want else 0}")
        if self._micro_want != self._micro_closed:
            self._ws.send(f"SET_MICRO:{1 if self._micro_want else 0}")
        self._redraw()
        self.after(REDRAW_MS, self._tick)

    def _redraw(self) -> None:
        with self._lock:
            t      = list(self._t)
            temp1  = list(self._bufs["temp1"])
            temp2  = list(self._bufs["temp2"])
            hum1   = list(self._bufs["hum1"])
            hum2   = list(self._bufs["hum2"])
            co2    = list(self._bufs["co2"])
            uv_irr = list(self._bufs["uv_irr"])
            uv_idx = list(self._bufs["uv_idx"])
            flow1       = list(self._bufs["flow1"])
            flow2       = list(self._bufs["flow2"])
            fluidTemp1  = list(self._bufs["fluidTemp1"])
            fluidTemp2  = list(self._bufs["fluidTemp2"])

        pal = _pal()

        # Microfluidics tab is independent of the incubator interlock
        if self._active == 2:
            if not self._micro_closed:
                self._flow_ax.cla()
                _style_ax(self._flow_ax, pal, "µL/min")
                self._flow_fig.patch.set_facecolor(pal["bg"])
                self._flow_canvas.draw_idle()
                self._flow1_val.configure(text="—")
                self._flow2_val.configure(text="—")
                self._fluidTemp1_val.configure(text="—")
                self._fluidTemp2_val.configure(text="—")
            else:
                _draw_series(self._flow_ax, t,
                             [(flow1, "Circuit 1", CLR["flow1"]),
                              (flow2, "Circuit 2", CLR["flow2"])],
                             pal, ylabel="µL/min")
                self._flow_fig.patch.set_facecolor(pal["bg"])
                self._flow_canvas.draw_idle()
                self._flow1_val.configure(text=f"{flow1[-1]:.1f}" if flow1 else "—")
                self._flow2_val.configure(text=f"{flow2[-1]:.1f}" if flow2 else "—")
                self._fluidTemp1_val.configure(text=f"{fluidTemp1[-1]:.1f}" if fluidTemp1 else "—")
                self._fluidTemp2_val.configure(text=f"{fluidTemp2[-1]:.1f}" if fluidTemp2 else "—")
            return

        # Incubator and UV tabs are gated by the incubator interlock
        if not self._incubator_closed:
            if self._active == 0:
                for ax, fig, canvas, lbl in [
                    (self._temp_ax, self._temp_fig, self._temp_canvas, "°C"),
                    (self._hum_ax,  self._hum_fig,  self._hum_canvas,  "%"),
                    (self._co2_ax,  self._co2_fig,  self._co2_canvas,  "%"),
                ]:
                    ax.cla()
                    _style_ax(ax, pal, lbl)
                    fig.patch.set_facecolor(pal["bg"])
                    canvas.draw_idle()
                self._temp1_val.configure(text="—")
                self._temp2_val.configure(text="—")
                self._hum1_val.configure(text="—")
                self._hum2_val.configure(text="—")
                self._co2_ppm_val.configure(text="—")
            elif self._active == 1:
                self._uv_ax.cla()
                _style_ax(self._uv_ax, pal, "W/m²")
                self._uv_fig.patch.set_facecolor(pal["bg"])
                self._uv_canvas.draw_idle()
                self._uvi_val.configure(text="—")
                self._irr_val.configure(text="—")
            return

        if self._active == 0:
            _draw_series(self._temp_ax, t,
                         [(temp1, "Top Sensor", CLR["temp1"]),
                          (temp2, "Bottom Sensor", CLR["temp2"])],
                         pal, ylabel="°C")
            self._temp_fig.patch.set_facecolor(pal["bg"])
            self._temp_canvas.draw_idle()
            self._temp1_val.configure(text=f"{temp1[-1]:.1f}" if temp1 else "—")
            self._temp2_val.configure(text=f"{temp2[-1]:.1f}" if temp2 else "—")

            _draw_series(self._hum_ax, t,
                         [(hum1, "Top Sensor", CLR["hum1"]),
                          (hum2, "Bottom Sensor", CLR["hum2"])],
                         pal, ylabel="%")
            self._hum_fig.patch.set_facecolor(pal["bg"])
            self._hum_canvas.draw_idle()
            self._hum1_val.configure(text=f"{hum1[-1]:.1f}" if hum1 else "—")
            self._hum2_val.configure(text=f"{hum2[-1]:.1f}" if hum2 else "—")

            _draw_series(self._co2_ax, t,
                         [(co2, "CO₂", CLR["co2"])],
                         pal, ylabel="%")
            self._co2_fig.patch.set_facecolor(pal["bg"])
            self._co2_canvas.draw_idle()
            last_co2 = co2[-1] if co2 else 0.0
            self._co2_ppm_val.configure(text=f"{last_co2 * 10000:.0f}")

        elif self._active == 1:
            _draw_series(self._uv_ax, t,
                         [(uv_irr, "Irradiance", CLR["uv"])],
                         pal, ylabel="W/m²")
            self._uv_fig.patch.set_facecolor(pal["bg"])
            self._uv_canvas.draw_idle()

            last_idx = uv_idx[-1] if uv_idx else 0.0
            last_irr = uv_irr[-1] if uv_irr else 0.0
            self._uvi_val.configure(text=f"{last_idx:.2f}")
            self._irr_val.configure(text=f"{last_irr:.4f}")

    # ── numeric-only StringVar factory ───────────────────────────────────────
    def _num_var(self) -> ctk.StringVar:
        sv = ctk.StringVar()
        _guard = [False]
        def _cb(*_):
            if _guard[0]:
                return
            raw = sv.get()
            clean = "".join(c for c in raw if c.isdigit() or c == ".")
            if clean.count(".") > 1:
                second_dot = clean.index(".", clean.index(".") + 1)
                clean = clean[:second_dot]
            if clean != raw:
                _guard[0] = True
                sv.set(clean)
                _guard[0] = False
            self._on_ctrl_change()
        sv.trace_add("write", _cb)
        return sv

    # ── send-button state (shared by all tabs) ────────────────────────────────
    def _update_send_btn(self) -> None:
        gate = self._micro_closed if self._active == 2 else self._incubator_closed
        if not gate:
            self._send_btn.configure(state="disabled",
                                     fg_color=_SEND_IDLE, hover_color=_SEND_IDLE_HOVER)
        elif self._pending_changes:
            self._send_btn.configure(state="normal",
                                     fg_color=_SEND_READY, hover_color=_SEND_READY_HOVER)
        else:
            self._send_btn.configure(state="normal",
                                     fg_color=_SEND_IDLE, hover_color=_SEND_IDLE_HOVER)

    # ── microfluidics safety toggle ───────────────────────────────────────────
    def _toggle_micro(self) -> None:
        self._micro_want = not self._micro_want
        self._micro_closed = self._micro_want
        if self._micro_closed:
            self._micro_btn.configure(
                text="Microfluidics Closed",
                fg_color="#1e8449", hover_color="#196f3d",
            )
        else:
            self._micro_btn.configure(
                text="Microfluidics Opened",
                fg_color="#c0392b", hover_color="#a93226",
            )
        self._ws.send(f"SET_MICRO:{1 if self._micro_want else 0}")
        self._update_send_btn()

    # ── pump mode toggle ──────────────────────────────────────────────────────
    def _on_pump_mode_change(self, pump_idx: int, mode: str) -> None:
        cont_frame, pulsed_frame = self._pump_frames[pump_idx]
        if mode == "Continuous":
            pulsed_frame.pack_forget()
            cont_frame.pack(fill="x")
        else:
            cont_frame.pack_forget()
            pulsed_frame.pack(fill="x")
        self._on_ctrl_change()

    # ── priming toggle ────────────────────────────────────────────────────────
    def _toggle_priming(self, pump_idx: int) -> None:
        self._priming_active[pump_idx] = not self._priming_active[pump_idx]
        active = self._priming_active[pump_idx]
        btn = self._priming_btns[pump_idx]
        if active:
            btn.configure(
                text="Priming  ON",
                fg_color="#d68910",
                hover_color="#b7770d",
                text_color="white",
            )
            # 400 Hz + maximum allowable voltage — firmware enforces voltage ceiling
            self._ws.send(f"SET_PRIMING:{pump_idx + 1}:1")
        else:
            btn.configure(
                text="Priming  OFF",
                fg_color=("gray78", "gray23"),
                hover_color=("gray68", "gray32"),
                text_color=("black", "white"),
            )
            # Restore previous pump configuration
            self._ws.send(f"SET_PRIMING:{pump_idx + 1}:0")
            self._resend_single_pump(pump_idx)

    # Re-sends configured pump parameters for one circuit after priming ends.
    def _resend_single_pump(self, pump_idx: int) -> None:
        pvars     = self._pump_vars[pump_idx]
        is_pulsed = 1 if self._pump_mode_vars[pump_idx].get() == "Pulsed" else 0
        if is_pulsed:
            volume_ul = float(pvars["volume"].get() or "0")
            feed_s    = float(pvars["feed"].get()   or "0")
            pause_s   = float(pvars["pause"].get()  or "0")
            flow_rate = (volume_ul / feed_s * 60.0) if feed_s > 0.0 else 0.0
            flow   = f"{flow_rate:.2f}"
            feed   = f"{feed_s:.2f}"
            pause  = f"{pause_s:.2f}"
            cycles = 0 if pvars["cycles_inf"].get() else int(pvars["cycles_n"].get() or "1")
        else:
            flow   = pvars["flow"].get() or "0"
            feed   = "0"
            pause  = "0"
            cycles = 0
        self._ws.send(
            f"SET_PUMP:{pump_idx + 1}:{flow}:{is_pulsed}:{feed}:{pause}:{cycles}"
        )

    # ── theme toggle ──────────────────────────────────────────────────────────
    def _toggle_theme(self) -> None:
        new = "Light" if ctk.get_appearance_mode() == "Dark" else "Dark"
        ctk.set_appearance_mode(new)
        self._theme_btn.configure(text="☀" if new == "Dark" else "🌙")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    App().mainloop()
