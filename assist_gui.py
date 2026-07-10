"""Stardew 자동낚시 — 보조모드 GUI (시작/종료 토글 버튼).

사용자가 직접 캐스팅+후킹, 봇은 미니게임 바만 제어. 완전자동 아님.
- [시작] 버튼을 누르면 감시 시작 → 버튼이 [종료]로 바뀜.
- [종료]를 누르면 정지 → 다시 [시작]으로.
- 전역 키후킹 없음 → ESC 등 다른 키/작업으로 절대 자동 종료되지 않음. 버튼으로만 제어.

실행:  pythonw assist_gui.py   (콘솔 없이)  또는  python assist_gui.py
"""
import os
import threading
import time
import ctypes
from datetime import datetime

import tkinter as tk

import numpy as np
import mss
import pydirectinput

from main import load_config, grab, detect, BarController, Mouse, _runtime_dir

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
        last_log = 0.0
        # 미니게임별 통계(문제 진단용): 프레임/물고기검출/추적오차/inside(바 안에 있던 비율)
        mg_frames = 0
        mg_fish = 0
        mg_inside = 0
        mg_errs = []
        mg_start = 0.0
        mg_no = 0

        # ---- 로그 파일 열기(항상 기록). logs/assist_YYYYMMDD_HHMMSS.log ----
        log = self._open_log(cfg)

        def summarize():
            if mg_frames <= 0:
                return
            dur = time.time() - mg_start
            if mg_errs:
                arr = sorted(mg_errs)
                med = arr[len(arr) // 2]
                mx = arr[-1]
                det = 100.0 * mg_fish / mg_frames
                inside = 100.0 * mg_inside / max(mg_fish, 1)   # 물고기가 바 안에 있던 비율
                # inside 로 잡음/놓침 추정(확정 아님 — 추후 진행바 판독으로 대체 예정)
                hint = "잡음추정" if inside >= 60 else ("애매" if inside >= 40 else "놓침추정")
                log("  << 미니게임#%d 종료 — %.1f초 %d프레임, 물고기검출 %.0f%%, inside %.0f%% [%s], 추적오차 median=%.0f max=%.0f"
                    % (mg_no, dur, mg_frames, det, inside, hint, med, mx))
            else:
                log("  << 미니게임#%d 종료 — %.1f초 %d프레임, 물고기 검출 0%% (색/위치 확인 필요)"
                    % (mg_no, dur, mg_frames))

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
                        if not in_prev:                       # 미니게임 시작 전이(transition)
                            in_prev = True
                            mg_no += 1
                            mg_frames = 0
                            mg_fish = 0
                            mg_inside = 0
                            mg_errs = []
                            mg_start = t0
                            log(">> 미니게임#%d 시작" % mg_no)
                        mg_frames += 1
                        bc = int(bar["center"])
                        fy = int(fish["center"]) if fish else None
                        if fy is not None:
                            mg_fish += 1
                            mg_errs.append(abs(bc - fy))
                            if bar["top"] <= fish["center"] <= bar["bottom"]:
                                mg_inside += 1                # 물고기가 초록 바 안에 있음
                        self.status = "미니게임 제어중  bar=%d fish=%s" % (bc, fy)
                        if t0 - last_log >= 0.1:              # 0.1초 간격 상세 로그
                            log("   bar=%d fish=%s vel=%.0f pred=%d %s"
                                % (bc, fy, ctrl.last_vel, int(ctrl.last_pred),
                                   "UP" if press else "DOWN"))
                            last_log = t0
                    elif in_prev and (t0 - bar_seen_t) < bar_grace:
                        # 순간 끊김(초록 flicker) — 리셋하지 말고 직전 동작 유지(연속성 보존)
                        mouse.set(last_press, t0, min_pulse)
                    else:
                        if in_prev:
                            in_prev = False
                            summarize()                       # 미니게임 종료 요약
                        ctrl.reset()
                        mouse.up()
                        self.status = "대기중 — 직접 캐스팅→'!' 후킹하세요"
                    el = time.time() - t0
                    if el < interval:
                        time.sleep(interval - el)
        finally:
            if in_prev:
                summarize()
            mouse.up()                              # 정지 시 반드시 버튼 떼기
            log("[%s] 세션 종료" % datetime.now().strftime("%H:%M:%S"))
            self._close_log()
            self.status = "정지"

    # ---------------------------------------------------------------- 로깅
    def _open_log(self, cfg):
        """세션 로그 파일을 열고 로그 함수를 반환. 항상 기록(문제 진단용)."""
        try:
            logdir = _runtime_dir() / "logs"
            os.makedirs(logdir, exist_ok=True)
            path = logdir / ("assist_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))
            self._logf = open(path, "w", encoding="utf-8")
            self.logpath = str(path)
        except Exception:
            self._logf = None
            self.logpath = None

        def log(msg):
            if self._logf is None:
                return
            try:
                self._logf.write(msg + "\n")
                self._logf.flush()
            except Exception:
                pass

        params = {k: cfg.get(k) for k in ("lookahead", "vel_window", "min_pulse", "bar_grace", "aim_offset", "fps")}
        log("[%s] 세션 시작  params=%s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), params))
        return log

    def _close_log(self):
        try:
            if getattr(self, "_logf", None):
                self._logf.close()
        except Exception:
            pass
        self._logf = None

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
