"""Stardew 자동낚시 — 보조모드 GUI (시작/종료 토글 버튼).

사용자가 직접 캐스팅+후킹, 봇은 미니게임 바만 제어. 완전자동 아님.
- [시작] 버튼을 누르면 감시 시작 → 버튼이 [종료]로 바뀜.
- [종료]를 누르면 정지 → 다시 [시작]으로.
- 전역 키후킹 없음 → ESC 등 다른 키/작업으로 절대 자동 종료되지 않음. 버튼으로만 제어.

실행:  pythonw assist_gui.py   (콘솔 없이)  또는  python assist_gui.py
"""
import threading
import time
import ctypes

import tkinter as tk

import numpy as np
import mss
import pydirectinput

from main import load_config, grab, detect, BarController, Mouse

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass


class AssistGUI:
    def __init__(self):
        self.cfg = load_config()
        self.running = False
        self.thread = None
        self.status = "정지"          # 워커가 갱신, GUI가 폴링해서 표시
        self._build_ui()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Stardew 자동낚시")
        self.root.resizable(False, False)
        self.root.geometry("250x130+30+30")         # 좌상단. 게임 뒤로 가도 됨(alt-tab으로 종료)

        self.btn = tk.Button(self.root, text="시작", command=self.toggle,
                             font=("맑은 고딕", 18, "bold"), bg="#2ecc71", fg="white",
                             activebackground="#27ae60", relief="flat", height=2)
        self.btn.pack(fill="x", padx=12, pady=(14, 8))

        self.lbl = tk.Label(self.root, text="정지 — [시작]을 누르세요",
                            font=("맑은 고딕", 9), fg="#555")
        self.lbl.pack()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll()

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.running:
            return
        self.running = True
        self.status = "대기중"
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()
        self.btn.config(text="종료", bg="#e74c3c", activebackground="#c0392b")

    def stop(self):
        self.running = False                       # 워커가 다음 루프에서 빠져나가며 마우스 놓음
        self.btn.config(text="시작", bg="#2ecc71", activebackground="#27ae60")

    def on_close(self):
        self.running = False
        self.root.after(150, self.root.destroy)

    # ---------------------------------------------------------------- 제어 루프(별도 스레드)
    def _worker(self):
        cfg = self.cfg
        mouse = Mouse()
        ctrl = BarController(cfg)
        interval = 1.0 / max(int(cfg["fps"]), 5)
        bar_grace = float(cfg.get("bar_grace", 0.35))
        min_pulse = float(cfg.get("min_pulse", 0.0))
        in_prev = False
        last_press = False
        bar_seen_t = 0.0
        try:
            with mss.mss() as sct:
                while self.running:
                    t0 = time.time()
                    frame = grab(sct, cfg["roi"])
                    bar, fish, _, _ = detect(frame, cfg)
                    if bar is not None:
                        bar_seen_t = t0
                        # focus_window 안 함: 보조모드는 사용자가 직접 플레이 중이라 게임이 이미
                        # 포그라운드다. 매 프레임 재포커스(ALT트릭)하면 게임이 멈춘다(freeze). 제거.
                        press = ctrl.update(t0, bar, fish)
                        last_press = press
                        mouse.set(press, t0, min_pulse)
                        in_prev = True
                        fy = int(fish["center"]) if fish else None
                        self.status = "미니게임 제어중  bar=%d fish=%s" % (int(bar["center"]), fy)
                    elif in_prev and (t0 - bar_seen_t) < bar_grace:
                        # 순간 끊김(초록 flicker) — 리셋하지 말고 직전 동작 유지(연속성 보존)
                        mouse.set(last_press, t0, min_pulse)
                    else:
                        if in_prev:
                            in_prev = False
                        ctrl.reset()
                        mouse.up()
                        self.status = "대기중 — 직접 캐스팅→'!' 후킹하세요"
                    el = time.time() - t0
                    if el < interval:
                        time.sleep(interval - el)
        finally:
            mouse.up()                              # 정지 시 반드시 버튼 떼기
            self.status = "정지"

    # ---------------------------------------------------------------- 상태 표시 폴링
    def _poll(self):
        if self.running:
            self.lbl.config(text=self.status, fg="#111")
        else:
            self.lbl.config(text="정지 — [시작]을 누르세요", fg="#555")
        self.root.after(120, self._poll)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    AssistGUI().run()
