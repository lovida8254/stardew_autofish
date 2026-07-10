"""
Stardew Valley 자동 낚시 봇 - GUI 버전 (완전 자동)
==================================================
실행:  python gui.py

기능:
  - 시작/정지 (전역 핫키 F8 동일)
  - 완전자동 체크박스: 캐스팅 → 입질 감지 → 후킹 → 미니게임까지 전자동
  - 캘리브레이션(트랙 영역) / 입질영역 캘리브레이션
  - 실시간 미리보기 (초록 바 / 물고기 인식 오버레이 + 상태)
  - kp/kd/deadzone/fps + 캐스팅/입질 파라미터 실시간 조정
  - 상태/로그 표시
"""

import time
import threading
import queue
import ctypes
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
import mss
import pydirectinput
from pynput import keyboard
from PIL import Image, ImageTk

from main import (load_config, save_config, grab, detect,
                  bite_change_fraction, FishingBrain, BarController, Mouse,
                  find_game_hwnd, focus_window)
from audio_bite import AudioBiteDetector

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False

PREVIEW_H = 440  # 미리보기 세로 크기(px)


# ================================================================ 봇 엔진 (백그라운드 스레드)

class BotEngine:
    def __init__(self, cfg: dict, log_fn):
        self.cfg = cfg
        self.log = log_fn
        self.enabled = False
        self.full_auto = bool(cfg.get("full_auto", False))
        self.stop_flag = False
        self.status = "대기 중"
        self.bite_frac = 0.0        # 최근 입질 변화량(화면 감지 디버그)
        self.bite_mode = cfg.get("bite_mode", "sound")
        self.mouse = Mouse()
        self.brain = FishingBrain(cfg)
        self.ctrl = BarController(cfg)
        self._bite_base = None
        self._bite_ready_t = 0.0
        self.audio = AudioBiteDetector(cfg, log=log_fn) if self.bite_mode == "sound" else None
        self._hwnd = None
        self.dbg = None            # (bar_y, fish_y, press) 실시간 진단
        # 진단 로그 파일 (미니게임 중 매 프레임 기록 → 원인 분석용)
        try:
            self._logf = open("fishlog.csv", "w", encoding="utf-8")
            self._logf.write("t,bar_y,fish_y,vel,pred,press,holding,downs,ups\n")
            self._log0 = None
        except Exception:
            self._logf = None
        self._snap_n = 0        # 저장한 진단 스냅샷 수
        self._snap_frame = 0
        self._lock = threading.Lock()
        self._preview = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ---- 외부 제어 ----
    def toggle(self):
        self.enabled = not self.enabled
        if not self.enabled:
            self.mouse.up()
            self.status = "일시정지"
        else:
            self.brain.reset()
            self.status = "실행 중"
        self.log(f"봇 {'시작' if self.enabled else '정지'}")

    def set_full_auto(self, on):
        self.full_auto = bool(on)
        self.brain.reset()
        self._bite_base = None
        self.mouse.up()
        self.log(f"완전자동 {'ON (캐스팅+입질+미니게임)' if on else 'OFF (미니게임만)'}")

    def set_bite_mode(self, mode):
        self.bite_mode = mode
        self.cfg["bite_mode"] = mode
        if mode == "sound" and self.audio is None:
            self.audio = AudioBiteDetector(self.cfg, log=self.log)
        if mode != "sound" and self.audio is not None:
            self.audio.arm(False)
        self.log(f"입질 감지: {'소리' if mode == 'sound' else '화면'}")

    def ensure_focus(self):
        """게임 창이 포그라운드가 아니면 포커스를 가져온다."""
        if not self.cfg.get("auto_focus", True):
            return
        if self._hwnd is None:
            self._hwnd = find_game_hwnd(self.cfg.get("game_title", "Stardew"))
        if self._hwnd and ctypes.windll.user32.GetForegroundWindow() != self._hwnd:
            focus_window(self._hwnd)

    def click_test(self):
        """게임을 포커스하고 1.2초간 좌클릭을 유지 → 미니게임 바가 올라가면 입력 정상."""
        self._hwnd = find_game_hwnd(self.cfg.get("game_title", "Stardew"))
        if not self._hwnd:
            self.log("게임 창을 못 찾음. game_title 확인 (기본 'Stardew')")
            return
        self.log("클릭 테스트: 1.2초 누름. 미니게임 바가 올라가면 입력 정상!")

        def run():
            focus_window(self._hwnd)
            time.sleep(0.15)
            self.mouse.down()
            time.sleep(1.2)
            self.mouse.up()
            self.log("클릭 테스트 끝")
        threading.Thread(target=run, daemon=True).start()

    def shutdown(self):
        self.stop_flag = True
        if self.audio is not None:
            self.audio.stop()
        self.mouse.up()
        if self._logf is not None:
            try:
                self._logf.close()
            except Exception:
                pass

    def get_preview(self):
        with self._lock:
            return None if self._preview is None else self._preview.copy()

    # ---- 완전자동 IO ----
    def _do_cast(self):
        cfg = self.cfg
        tgt = cfg.get("cast_target")
        if tgt:
            self.mouse.move(tgt["x"], tgt["y"])
            time.sleep(0.05)
        self.mouse.click(hold=float(cfg["cast_power"]))
        self._bite_base = None
        self._bite_ready_t = time.time() + float(cfg["cast_settle"])

    def _read_bite(self, sct, now):
        if now < self._bite_ready_t:
            return False
        try:
            cur = grab(sct, self.cfg["bite_roi"])
        except Exception:
            return False
        if self._bite_base is None:
            self._bite_base = cur
            return False
        self.bite_frac = bite_change_fraction(self._bite_base, cur)
        return self.bite_frac > float(self.cfg["bite_change"])

    def _apply(self, action):
        if action == "cast":
            self.status = "🎣 캐스팅"
            self.log("캐스팅")
            self._do_cast()
        elif action == "hook":
            self.status = "❗ 입질 - 후킹!"
            self.log("입질! 후킹")
            self.mouse.click()
        elif action == "recast":
            self.log("입질 없음 → 재캐스팅")
        elif action == "press":
            self.mouse.down()
        elif action == "release":
            self.mouse.up()
        elif action == "click":
            self.status = "보상 확인 → 다음"
            self.log("잡았다! 다음 낚시")
            self.mouse.click()
        # 'hold', 'none': 유지

    # ---- 메인 루프 ----
    def _loop(self):
        STATE_KO = {"cast": "캐스팅 준비", "wait": "입질 대기 중",
                    "fight": "🐟 미니게임 진행!", "reward": "보상 확인"}
        with mss.mss() as sct:
            while not self.stop_flag:
                t0 = time.time()
                cfg = self.cfg
                interval = 1.0 / max(int(cfg["fps"]), 5)
                try:
                    frame = grab(sct, cfg["roi"])
                except Exception:
                    time.sleep(0.2)
                    continue

                bar, fish, _, _ = detect(frame, cfg)

                if self.enabled and (self.full_auto or bar is not None):
                    self.ensure_focus()

                # ---- 미리보기 오버레이 ----
                view = np.ascontiguousarray(frame)
                if bar:
                    cv2.rectangle(view, (0, int(bar["top"])),
                                  (view.shape[1] - 1, int(bar["bottom"])), (0, 255, 0), 2)
                if fish:
                    cv2.circle(view, (view.shape[1] // 2, int(fish["center"])), 7, (0, 0, 255), 2)
                with self._lock:
                    self._preview = view

                if not self.enabled:
                    self.status = "일시정지"
                    time.sleep(interval)
                    continue

                if self.full_auto:
                    bite = False
                    if self.brain.state == "wait":
                        if self.bite_mode == "sound" and self.audio is not None:
                            if t0 >= self._bite_ready_t:
                                self.audio.arm(True)
                                bite = self.audio.consume_spike()
                        else:
                            bite = self._read_bite(sct, t0)
                    elif self.audio is not None:
                        self.audio.arm(False)
                    action = self.brain.step(t0, bar, fish, bite)
                    self._apply(action)
                    self.status = STATE_KO.get(self.brain.state, self.brain.state)
                else:
                    # 미니게임 보조 전용 (예측 제어 + 최소 펄스)
                    if bar is not None:
                        press = self.ctrl.update(t0, bar, fish)
                        self.mouse.set(press, t0, float(cfg.get("min_pulse", 0.0)))
                        self.dbg = (int(bar["center"]), int(fish["center"]) if fish else None, press)
                        self.status = "🐟 미니게임 진행!"
                        # 진단 스냅샷: 오버레이(초록바/빨간 물고기) 몇 장 저장
                        self._snap_frame += 1
                        if self._snap_n < 12 and self._snap_frame % 15 == 0:
                            big = cv2.resize(view, (view.shape[1] * 5, view.shape[0]),
                                             interpolation=cv2.INTER_NEAREST)
                            cv2.imwrite("roi_dbg_%02d.png" % self._snap_n, big)
                            self._snap_n += 1
                        if self._logf is not None:
                            if self._log0 is None:
                                self._log0 = t0
                            self._logf.write("%.3f,%.0f,%s,%.0f,%.0f,%d,%d,%d,%d\n" % (
                                t0 - self._log0, bar["center"],
                                ("%.0f" % fish["center"]) if fish else "",
                                self.ctrl.last_vel, self.ctrl.last_pred,
                                int(press), int(self.mouse.holding),
                                self.mouse.downs, self.mouse.ups))
                            self._logf.flush()
                    else:
                        self.ctrl.reset()
                        self.mouse.up()
                        self.dbg = None
                        self.status = "실행 중 (미니게임 대기)"

                elapsed = time.time() - t0
                if elapsed < interval:
                    time.sleep(interval - elapsed)
        self.mouse.up()


# ================================================================ GUI

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Stardew 자동 낚시")
        root.geometry("820x680")
        root.minsize(720, 600)

        self.cfg = load_config()
        self.log_q = queue.Queue()
        self.engine = BotEngine(self.cfg, self.log_q.put)
        self._tk_img = None

        self._build_ui()
        self._start_hotkey()
        self._tick()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------ UI
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        # 미리보기
        left = ttk.LabelFrame(main, text="실시간 미리보기", padding=6)
        left.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 10))
        self.preview_label = ttk.Label(left, text="캘리브레이션 후\n트랙 화면 표시", anchor="center")
        self.preview_label.pack(fill="both", expand=True, ipadx=30, ipady=100)

        # 상단: 상태 + 버튼
        top = ttk.Frame(main)
        top.grid(row=0, column=1, sticky="ew")
        top.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(top, textvariable=self.status_var,
                  font=("Malgun Gothic", 14, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.btn_toggle = ttk.Button(top, text="▶ 시작 (F8)", command=self.engine.toggle, width=14)
        self.btn_toggle.grid(row=1, column=0, sticky="w")

        self.auto_var = tk.BooleanVar(value=self.engine.full_auto)
        ttk.Checkbutton(top, text="완전자동 (F9)", variable=self.auto_var,
                        command=self._on_auto).grid(row=1, column=1, padx=8)
        ttk.Button(top, text="💾 저장", command=self.save, width=8).grid(row=1, column=2)

        ttk.Button(top, text="🎯 트랙 영역", command=lambda: self.calibrate("roi"),
                   width=14).grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(top, text="❗ 입질 영역", command=lambda: self.calibrate("bite_roi"),
                   width=14).grid(row=2, column=1, padx=8, pady=(6, 0))

        modef = ttk.Frame(top)
        modef.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(modef, text="🖱 클릭 테스트", command=self.engine.click_test,
                   width=13).pack(side="left")
        ttk.Label(modef, text="  입질 감지:").pack(side="left")
        self.mode_var = tk.StringVar(value="소리" if self.engine.bite_mode == "sound" else "화면")
        cb = ttk.Combobox(modef, textvariable=self.mode_var, values=["소리", "화면"],
                          width=6, state="readonly")
        cb.pack(side="left", padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", self._on_mode)
        self.bite_var = tk.StringVar(value="")
        ttk.Label(modef, textvariable=self.bite_var, foreground="#c0392b").pack(side="left")

        self.dbg_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.dbg_var, font=("Consolas", 11, "bold"),
                  foreground="#1565c0").grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # 파라미터
        params = ttk.LabelFrame(main, text="파라미터", padding=8)
        params.grid(row=1, column=1, sticky="nsew", pady=(10, 0))
        params.columnconfigure(1, weight=1)

        self.vars = {}
        rows = [
            ("lookahead", "예측 시간 (일찍 뗌)", 0.0, 0.5),
            ("aim_offset", "조준 보정 (px)", -60, 60),
            ("fps", "처리 속도 (fps)", 30, 90),
            ("cast_power", "캐스팅 힘 (초)", 0.02, 1.0),
            ("wait_timeout", "입질 대기 한도 (초)", 5, 60),
            ("bite_sound_ratio", "입질 소리 배수", 1.5, 8.0),
            ("bite_sound_floor", "입질 소리 바닥값", 0.005, 0.2),
        ]
        for i, (key, label, lo, hi) in enumerate(rows):
            ttk.Label(params, text=label).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.DoubleVar(value=float(self.cfg[key]))
            self.vars[key] = var
            ttk.Scale(params, from_=lo, to=hi, variable=var,
                      command=lambda v, k=key: self._on_param(k)).grid(row=i, column=1, sticky="ew", padx=8)
            lbl = ttk.Label(params, width=6)
            lbl.grid(row=i, column=2)
            self.vars[key + "_lbl"] = lbl

        # HSV 색상 범위 (하한 / 상한)
        adv = ttk.LabelFrame(params, text="고급: 색상 HSV (하한 / 상한)", padding=6)
        adv.grid(row=len(rows), column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.adv_entries = {}
        for r, (name, prefix) in enumerate([("초록 바", "bar_hsv"), ("물고기(청록)", "fish_hsv")]):
            ttk.Label(adv, text=name).grid(row=r, column=0, sticky="w")
            for c, bound in enumerate(["lower", "upper"]):
                k = f"{prefix}_{bound}"
                var = tk.StringVar(value=",".join(map(str, self.cfg[k])))
                e = ttk.Entry(adv, textvariable=var, width=13)
                e.grid(row=r, column=1 + c, padx=3, pady=2)
                var.trace_add("write", lambda *a, k=k, v=var: self._on_hsv(k, v))
                self.adv_entries[k] = var

        # 로그
        logf = ttk.LabelFrame(main, text="로그", padding=4)
        logf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.log_text = tk.Text(logf, height=6, state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="x")

        self._log("시작. 1920x1080 창모드 기본값 적용됨. 좌표가 다르면 '트랙 영역'을 다시 잡으세요.")
        self._log("완전자동을 켜기 전에 게임 창을 포커스하고, 커서를 물 위에 두세요.")

    # ------------------------------------------------ 이벤트
    def _on_param(self, key):
        v = self.vars[key].get()
        if key in ("fps", "wait_timeout"):
            v = int(round(v))
        else:
            v = round(v, 3)
        self.cfg[key] = v

    def _on_hsv(self, key, var):
        try:
            vals = [int(x) for x in var.get().split(",")]
            if len(vals) == 3:
                self.cfg[key] = vals
        except ValueError:
            pass

    def _on_int(self, key, var):
        try:
            self.cfg[key] = int(var.get())
        except ValueError:
            pass

    def _on_auto(self):
        self.engine.set_full_auto(self.auto_var.get())

    def _on_mode(self, _e=None):
        self.engine.set_bite_mode("sound" if self.mode_var.get() == "소리" else "vision")

    def save(self):
        self.cfg["full_auto"] = self.engine.full_auto
        save_config(self.cfg)
        self._log("설정 저장 (config.json)")

    # ------------------------------------------------ 캘리브레이션 (Tk 오버레이, 메인 스레드)
    def calibrate(self, target):
        if self.engine.enabled:
            self.engine.toggle()
        label = "트랙(세로 게이지)" if target == "roi" else "입질(!) 감지 영역 (찌 위쪽 물)"
        self._log(f"3초 후 캡처. {label}을 드래그로 선택하세요.")

        def countdown(n):
            if n > 0:
                self.status_var.set(f"캘리브레이션: {n}초...")
                self.root.after(1000, countdown, n - 1)
            else:
                self._overlay(target, label)

        countdown(3)

    def _overlay(self, target, label):
        with mss.mss() as sct:
            mon = sct.monitors[1]
            shot = np.array(sct.grab(mon))[:, :, :3]
        pil = Image.fromarray(cv2.cvtColor(shot, cv2.COLOR_BGR2RGB))

        win = tk.Toplevel(self.root)
        win.attributes("-fullscreen", True)
        win.attributes("-topmost", True)
        win.configure(cursor="crosshair")
        canvas = tk.Canvas(win, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        tk_img = ImageTk.PhotoImage(pil)
        canvas.create_image(0, 0, anchor="nw", image=tk_img)
        canvas.image = tk_img
        canvas.create_text(mon["width"] // 2, 28,
                           text=f"{label} 을 드래그하세요   (ESC = 취소)",
                           fill="#ffdd00", font=("Malgun Gothic", 16, "bold"))

        st = {"x0": None, "y0": None, "rect": None}

        def on_down(e):
            st["x0"], st["y0"] = e.x, e.y
            if st["rect"]:
                canvas.delete(st["rect"])
            st["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="lime", width=2)

        def on_drag(e):
            if st["rect"] is not None:
                canvas.coords(st["rect"], st["x0"], st["y0"], e.x, e.y)

        def on_up(e):
            if st["x0"] is None:
                return
            left, top = min(st["x0"], e.x), min(st["y0"], e.y)
            w, h = abs(e.x - st["x0"]), abs(e.y - st["y0"])
            win.destroy()
            if w > 4 and h > 4:
                self.cfg[target] = {"left": int(left) + mon["left"], "top": int(top) + mon["top"],
                                    "width": int(w), "height": int(h)}
                save_config(self.cfg)
                self._log(f"{target} 저장: {self.cfg[target]}")
            else:
                self._log("영역이 너무 작아 취소됨")

        def on_cancel(_e=None):
            win.destroy()
            self._log("캘리브레이션 취소됨")

        canvas.bind("<ButtonPress-1>", on_down)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_up)
        win.bind("<Escape>", on_cancel)
        win.focus_force()

    # ------------------------------------------------ 핫키
    def _start_hotkey(self):
        def on_press(key):
            if key == keyboard.Key.f8:
                self.engine.toggle()
            elif key == keyboard.Key.f9:
                self.auto_var.set(not self.auto_var.get())
                self._on_auto()
        self.listener = keyboard.Listener(on_press=on_press)
        self.listener.start()

    # ------------------------------------------------ 주기 갱신
    def _tick(self):
        self.status_var.set(self.engine.status)
        self.btn_toggle.config(text="■ 정지 (F8)" if self.engine.enabled else "▶ 시작 (F8)")
        if self.auto_var.get() != self.engine.full_auto:
            self.auto_var.set(self.engine.full_auto)

        if self.engine.bite_mode == "sound":
            a = self.engine.audio
            if a is None or not a.ok:
                self.bite_var.set("소리 감지 준비 중...")
            else:
                trig = float(self.cfg["bite_sound_floor"])
                base_trig = a.baseline * float(self.cfg["bite_sound_ratio"])
                self.bite_var.set(f"음량 {a.level:.3f} / 발동 {max(trig, base_trig):.3f}")
        elif self.engine.full_auto and self.engine.brain.state == "wait":
            self.bite_var.set(f"화면 변화량 {self.engine.bite_frac:.2f} / 기준 {float(self.cfg['bite_change']):.2f}")
        else:
            self.bite_var.set("")

        for key in ("lookahead", "aim_offset", "fps", "cast_power",
                    "bite_sound_ratio", "bite_sound_floor", "wait_timeout"):
            v = self.cfg[key]
            self.vars[key + "_lbl"].config(text=f"{v:g}")

        dbg = self.engine.dbg
        if dbg is not None:
            bar_y, fish_y, press = dbg
            arrow = "▲누름(올림)" if press else "▽뗌(내림)"
            if fish_y is None:
                self.dbg_var.set(f"바 {bar_y}  물고기 ?    {arrow}")
            else:
                rel = "물고기 아래" if bar_y > fish_y else "물고기 위"
                self.dbg_var.set(f"바 {bar_y}  물고기 {fish_y}  (바가 {rel})  {arrow}")
        else:
            self.dbg_var.set("")

        frame = self.engine.get_preview()
        if frame is not None:
            h, w = frame.shape[:2]
            scale = PREVIEW_H / h
            small = cv2.resize(frame, (max(int(w * scale), 1), PREVIEW_H), interpolation=cv2.INTER_NEAREST)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            self._tk_img = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.preview_label.config(image=self._tk_img, text="")

        while not self.log_q.empty():
            self._log(self.log_q.get())

        self.root.after(50, self._tick)

    def _log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def on_close(self):
        self.engine.shutdown()
        self.root.destroy()


# ================================================================ 실행

if __name__ == "__main__":
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
