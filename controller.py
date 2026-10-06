import copy
import hmac
import json
import math
import os
import secrets
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

from flask import Flask, jsonify, request
from waitress import serve


# ---------- ตั้งค่าแมพ (ใช้ Place ID ทั้ง 2 แมพตามเดิม) ----------
MAPS = {
    "แมพแถวแรก (Place 1)": "120651982896178",
    "Sky Film (Place 2)": "77210125175879",
}

HOST = "127.0.0.1"
PORT = 8888  # เปลี่ยนพอร์ตหนีการชน

DATA_FILE = Path(__file__).resolve().with_name("controller_state.json")
LOCK = threading.RLock()


# ---------- ระบบจัดการไฟล์สถานะ ----------
def save_locked():
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(DATA, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, DATA_FILE)


if DATA_FILE.exists():
    DATA = json.loads(DATA_FILE.read_text(encoding="utf-8"))
else:
    DATA = {
        "token": secrets.token_urlsafe(32),
        "places": {},
    }

for place_id in MAPS.values():
    DATA["places"].setdefault(
        place_id,
        {
            "mode": "open",
            "reason": "เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง",
            "deadline": None,
        },
    )

with LOCK:
    save_locked()


def reconcile_locked():
    now = time.time()
    changed = False
    for state in DATA["places"].values():
        if (
            state["mode"] == "scheduled"
            and state["deadline"] is not None
            and now >= state["deadline"]
        ):
            state["mode"] = "closed"
            state["deadline"] = None
            changed = True
    if changed:
        save_locked()


def snapshot(place_id):
    with LOCK:
        reconcile_locked()
        result = copy.deepcopy(DATA["places"][place_id])
        result["serverNow"] = time.time()
        return result


def update_state(place_id, mode, reason=None, seconds=None):
    with LOCK:
        reconcile_locked()
        state = DATA["places"][place_id]
        state["mode"] = mode
        state["deadline"] = (
            time.time() + seconds if mode == "scheduled" else None
        )
        if reason is not None:
            state["reason"] = reason
        save_locked()


# ---------- Flask API Server ----------
app = Flask(__name__)


@app.get("/state/<place_id>")
def get_state(place_id):
    expected = "Bearer " + DATA["token"]
    supplied = request.headers.get("Authorization", "")

    if not hmac.compare_digest(
        supplied.encode("utf-8"),
        expected.encode("utf-8"),
    ):
        return jsonify({"error": "unauthorized"}), 401

    if place_id not in MAPS.values():
        return jsonify({"error": "unknown place"}), 404

    response = jsonify(snapshot(place_id))
    response.headers["Cache-Control"] = "no-store"
    return response


def run_api():
    serve(app, host=HOST, port=PORT, threads=8)


# ---------- สร้างหน้าต่าง GUI (Modern Dark Theme) ----------
root = tk.Tk()
root.title("Roblox Local Server Controller")
root.geometry("750x600")
root.configure(bg="#1e1e2f")
root.resizable(False, False)

style = ttk.Style()
style.theme_use("clam")
style.configure("TFrame", background="#1e1e2f")
style.configure("TLabelframe", background="#1e1e2f", foreground="#ffffff", relief="flat")
style.configure("TLabelframe.Label", background="#1e1e2f", foreground="#7692ff", font=("Segoe UI", 11, "bold"))
style.configure("TLabel", background="#1e1e2f", foreground="#d1d1e0", font=("Segoe UI", 10))
style.configure("Header.TLabel", background="#1e1e2f", foreground="#ffffff", font=("Segoe UI", 16, "bold"))
style.configure("Status.TLabel", background="#2a2a40", foreground="#00ffcc", font=("Segoe UI", 11, "bold"))
style.configure("TButton", background="#3f3f5f", foreground="#ffffff", font=("Segoe UI", 10, "bold"), borderwidth=0)
style.map("TButton", background=[("active", "#52527a"), ("disabled", "#2a2a3a")], foreground=[("disabled", "#666666")])
style.configure("Action.TButton", background="#ff4d4d", foreground="#ffffff")
style.configure("Open.TButton", background="#00b894", foreground="#ffffff")

frame = ttk.Frame(root, padding=20)
frame.pack(fill="both", expand=True)

ttk.Label(frame, text="🛡️ Roblox Local Control Center", style="Header.TLabel").pack(anchor="w", pady=(0, 15))

control_frame = ttk.LabelFrame(frame, text=" ตั้งค่าการควบคุม ", padding=15)
control_frame.pack(fill="x", pady=(0, 15))

ttk.Label(control_frame, text="เลือกแมพ:").grid(row=0, column=0, sticky="w", pady=5)
selected_map = tk.StringVar(value=next(iter(MAPS)))
map_box = ttk.Combobox(control_frame, textvariable=selected_map, values=list(MAPS), state="readonly", width=45)
map_box.grid(row=0, column=1, sticky="ew", padx=10, pady=5)

ttk.Label(control_frame, text="เหตุผล / ข้อความเตะ:").grid(row=1, column=0, sticky="w", pady=5)
reason_var = tk.StringVar(value="เซิร์ฟเวอร์ปิดปรับปรุง กรุณาเข้าใหม่ภายหลัง")
reason_entry = ttk.Entry(control_frame, textvariable=reason_var, width=48)
reason_entry.grid(row=1, column=1, sticky="ew", padx=10, pady=5)

timer_enabled = tk.BooleanVar(value=True)
ttk.Checkbutton(control_frame, text="นับถอยหลังก่อนปิด", variable=timer_enabled).grid(row=2, column=1, sticky="w", padx=10, pady=5)

ttk.Label(control_frame, text="เวลา (วินาที):").grid(row=3, column=0, sticky="w", pady=5)
seconds_var = tk.StringVar(value="60")
seconds_entry = ttk.Entry(control_frame, textvariable=seconds_var, width=15)
seconds_entry.grid(row=3, column=1, sticky="w", padx=10, pady=5)

status_frame = ttk.LabelFrame(frame, text=" สถานะระบบปัจจุบัน ", padding=15)
status_frame.pack(fill="x", pady=(0, 15))

status_var = tk.StringVar()
ttk.Label(status_frame, textvariable=status_var, style="Status.TLabel").pack(anchor="w", pady=(0, 5))

countdown_var = tk.StringVar(value="⏱️ —")
ttk.Label(status_frame, textvariable=countdown_var, font=("Segoe UI", 18, "bold"), foreground="#ffa502", background="#1e1e2f").pack(anchor="w")

buttons_frame = ttk.Frame(frame)
buttons_frame.pack(fill="x", pady=(0, 15))


def current_place_id():
    return MAPS[selected_map.get()]


def load_form(_event=None):
    st = snapshot(current_place_id())
    reason_var.set(st["reason"])


def start_close():
    reason = reason_var.get().strip()
    if not reason:
        messagebox.showerror("ข้อมูลไม่ครบ", "กรุณาระบุเหตุผล")
        return

    pid = current_place_id()
    if timer_enabled.get():
        try:
            secs = int(seconds_var.get())
            if secs < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("เวลาไม่ถูกต้อง", "ใส่ตัวเลขวินาทีตั้งแต่ 1 ขึ้นไป")
            return
        update_state(pid, "scheduled", reason, secs)
        notice_var.set("ตั้งเวลาปิดแล้ว รอเกมดึงสถานะ")
    else:
        if not messagebox.askyesno("ยืนยัน", f"ปิด {selected_map.get()} ทันทีใช่หรือไม่?"):
            return
        update_state(pid, "closed", reason)
        notice_var.set("สั่งปิดแมพแล้ว")


def cancel_countdown():
    pid = current_place_id()
    if snapshot(pid)["mode"] != "scheduled":
        messagebox.showinfo("แจ้งเตือน", "ไม่มีเวลานับถอยหลังที่กำลังรันอยู่")
        return
    update_state(pid, "open")
    notice_var.set("ยกเลิกนับถอยหลังแล้ว")


def open_map():
    update_state(current_place_id(), "open")
    notice_var.set("เปิดแมพเรียบร้อย")


def copy_token():
    root.clipboard_clear()
    root.clipboard_append(DATA["token"])
    notice_var.set("คัดลอก API Token เรียบร้อย (นำไปใส่ในสคริปต์ Roblox)")


for text, cmd, style_name in [
    ("🛑 เริ่มปิด", start_close, "Action.TButton"),
    ("↩️ ยกเลิก", cancel_countdown, "TButton"),
    ("✅ เปิดแมพ", open_map, "Open.TButton"),
    ("📋 คัดลอก Token", copy_token, "TButton"),
]:
    ttk.Button(buttons_frame, text=text, command=cmd, style=style_name).pack(side="left", ipadx=8, ipady=6, padx=(0, 6))

notice_var = tk.StringVar(value="พร้อมใช้งาน")
ttk.Label(frame, textvariable=notice_var, foreground="#00d2d3", wraplength=700).pack(anchor="w")


def tick():
    st = snapshot(current_place_id())
    labels = {"open": "🟢 เปิดให้บริการ", "scheduled": "⏳ กำลังนับถอยหลัง", "closed": "🔴 ปิดปรับปรุง"}
    status_var.set(f"สถานะ: {labels[st['mode']]} | Place ID: {current_place_id()}")

    if st["mode"] == "scheduled":
        rem = max(0, math.ceil(st["deadline"] - st["serverNow"]))
        countdown_var.set(f"⏱️ ปิดในอีก: {rem // 60:02d}:{rem % 60:02d}")
    elif st["mode"] == "closed":
        countdown_var.set("🔴 แมพปิดอยู่ (ผู้เล่นจะถูกเตะ)")
    else:
        countdown_var.set("⏱️ พร้อมทำงาน")
    root.after(200, tick)


map_box.bind("<<ComboboxSelected>>", load_form)
threading.Thread(target=run_api, daemon=True).start()

load_form()
tick()
root.mainloop()