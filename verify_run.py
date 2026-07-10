"""라이브 검증 러너 (완전자동, 헤드리스)
==========================================
gui.py 창을 열지 않고 main.py 로직만 재사용해서 완전자동으로 낚시한다:
  캐스팅 → 입질(소리) 감지 → 후킹 → 미니게임(바 제어) → 보상 → 재캐스팅.
BotEngine 의 full_auto 동작을 그대로 옮기고, 미니게임 중에는 fishlog.csv 와
오버레이 스냅샷(roi_dbg_*.png)을 남겨 진단할 수 있게 한다.

실행:  python verify_run.py       (ESC 로 종료)
시작 즉시 첫 캐스팅을 하므로, 캐릭터가 낚싯대를 들고 물을 향한 상태로 두세요.
"""

import time
import ctypes

import cv2
import numpy as np
import mss
import pydirectinput
from pynput import keyboard

from main import (load_config, grab, detect, FishingBrain, Mouse,
                  bite_change_fraction, find_game_hwnd, focus_window)
from audio_bite import AudioBiteDetector

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass


class Runner:
    def __init__(self):
        self.cfg = load_config()
        self.mouse = Mouse()
        self.brain = FishingBrain(self.cfg)
        self.hwnd = find_game_hwnd(self.cfg.get("game_title", "Stardew"))
        self.bite_mode = self.cfg.get("bite_mode", "sound")
        self.audio = AudioBiteDetector(self.cfg, log=lambda m: print(f"[오디오] {m}")) \
            if self.bite_mode == "sound" else None
        self._bite_base = None
        self._bite_ready_t = 0.0
        self.quit = False
        # 진단 로그
        self.logf = open("fishlog.csv", "w", encoding="utf-8")
        self.logf.write("t,bar_y,fish_y,vel,pred,press,holding,downs,ups\n")
        self._log0 = None
        self._snap_n = 0
        self._snap_frame = 0
        self._last_print = 0.0

    def ensure_focus(self):
        if not self.cfg.get("auto_focus", True):
            return
        if self.hwnd and ctypes.windll.user32.GetForegroundWindow() != self.hwnd:
            focus_window(self.hwnd)

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
        return bite_change_fraction(self._bite_base, cur) > float(self.cfg["bite_change"])

    def _apply(self, action):
        if action == "cast":
            print("[verify] >> 캐스팅")
            self._do_cast()
        elif action == "hook":
            print("[verify] >> 입질! 후킹")
            self.mouse.click()
        elif action == "recast":
            print("[verify] >> 입질 없음 → 재캐스팅")
        elif action == "press":
            self.mouse.down()
        elif action == "release":
            self.mouse.up()
        elif action == "click":
            print("[verify] >> 잡았다! 보상 확인 → 다음")
            self.mouse.click()

    def run(self):
        cfg = self.cfg
        interval = 1.0 / max(int(cfg["fps"]), 5)

        def on_key(key):
            if key == keyboard.Key.esc:
                self.quit = True
                return False
        listener = keyboard.Listener(on_press=on_key)
        listener.start()

        # 안전 한도: 아래 도달 시 정상 종료(마우스 up 후 정리). 사용자는 언제든 ESC.
        MAX_SEC = 220.0
        MAX_CATCHES = 3
        catches = 0
        t_run0 = time.time()

        print("[verify] 완전자동 시작. 캐릭터가 낚싯대 들고 물 향한 상태여야 함. ESC=종료.")
        print("[verify] 안전한도: %.0fs 또는 %d마리 잡으면 자동 종료." % (MAX_SEC, MAX_CATCHES))
        self.ensure_focus()
        time.sleep(0.3)

        with mss.mss() as sct:
            while not self.quit:
                t0 = time.time()
                if t0 - t_run0 > MAX_SEC:
                    print("[verify] 시간 한도 도달 → 종료")
                    break
                if catches >= MAX_CATCHES:
                    print("[verify] %d마리 잡음 → 종료" % catches)
                    break
                frame = grab(sct, cfg["roi"])
                bar, fish, _, _ = detect(frame, cfg)

                # 입력이 필요한 상황이면 게임 창을 포그라운드로
                self.ensure_focus()

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
                if action == "click":   # 보상창 닫기 = 한 마리 잡음
                    catches += 1

                # 미니게임 중이면 진단 기록
                if bar is not None:
                    press = (action == "press")
                    ctrl = self.brain.ctrl
                    view = np.ascontiguousarray(frame)
                    cv2.rectangle(view, (0, int(bar["top"])),
                                  (view.shape[1] - 1, int(bar["bottom"])), (0, 255, 0), 2)
                    if fish:
                        cv2.circle(view, (view.shape[1] // 2, int(fish["center"])), 7, (0, 0, 255), 2)
                    self._snap_frame += 1
                    if self._snap_n < 12 and self._snap_frame % 15 == 0:
                        big = cv2.resize(view, (view.shape[1] * 5, view.shape[0]),
                                         interpolation=cv2.INTER_NEAREST)
                        cv2.imwrite("roi_dbg_%02d.png" % self._snap_n, big)
                        self._snap_n += 1
                    if self._log0 is None:
                        self._log0 = t0
                    self.logf.write("%.3f,%.0f,%s,%.0f,%.0f,%d,%d,%d,%d\n" % (
                        t0 - self._log0, bar["center"],
                        ("%.0f" % fish["center"]) if fish else "",
                        ctrl.last_vel, ctrl.last_pred,
                        int(press), int(self.mouse.holding), self.mouse.downs, self.mouse.ups))
                    self.logf.flush()
                    if t0 - self._last_print > 0.25:
                        fy = int(fish["center"]) if fish else None
                        print("[verify] bar=%3d fish=%s vel=%5.0f pred=%3d %s" % (
                            int(bar["center"]), fy, ctrl.last_vel, int(ctrl.last_pred),
                            "▲누름" if press else "▽뗌"))
                        self._last_print = t0

                elapsed = time.time() - t0
                if elapsed < interval:
                    time.sleep(interval - elapsed)

        self.mouse.up()
        if self.audio is not None:
            self.audio.stop()
        self.logf.close()
        listener.stop()
        print("[verify] 종료.")


if __name__ == "__main__":
    Runner().run()
