"""바(bar) 물리 계측기 — sim_tune.py 의 물리(G, VMAX) 실측 보정용.

빠른 물고기가 아니라 '흔한 물고기' 미니게임에서 돌리면 된다(바 물리는 물고기와 무관).
물고기는 무시하고 정해진 사각파(누름 HOLD_S초 → 뗌 HOLD_S초 반복)를 넣으며 bar_y를 로깅한다.
끝나면(ESC 또는 미니게임 종료) SDV식 물리 모델의 두 상수를 자동 추정해 출력한다:
  - G    : 가속도 크기(px/s^2). 누름/뗌 시 바 속도가 변하는 비율(사각파 전환 직후 기울기).
  - VMAX : 속도 한계(px/s). 오래 누르거나 떼고 있을 때 도달하는 최대 정상속도.
그리고 sim_tune.py 의 PHYS_ANCHORS 에 바로 붙여넣을 (G, VMAX) 한 줄을 출력한다.

주의: 봇이 마우스로 게임을 조작한다(낮/전체화면, 미니게임 떠 있을 때). ESC 종료.
실행:  python -u calib_bar.py
"""
import sys
import time
import ctypes
import numpy as np
import mss
import pydirectinput
from pynput import keyboard
from main import load_config, grab, detect, Mouse, find_game_hwnd, focus_window

try:
    sys.stdout.reconfigure(encoding="utf-8")   # cp949 콘솔에서 기호/한글 인코딩 크래시 방지
except Exception:
    pass

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

HOLD_S = 1.1             # 사각파 반주기. 길게=트랙을 길게 훑는 깨끗한 가속 램프(G 정밀측정용)
BAR_H = 100.0           # 바 세로길이(px) — 벽 근처 표본 제외용(sim_tune 와 동일 가정)

cfg = load_config()
mouse = Mouse()
hwnd = find_game_hwnd(cfg.get("game_title", "Stardew"))
interval = 1.0 / max(int(cfg["fps"]), 5)
track_h = float(cfg["roi"]["height"])
quit = {"q": False}


def on_key(k):
    if k == keyboard.Key.esc:
        quit["q"] = True
        return False


keyboard.Listener(on_press=on_key).start()
print("[calib] 흔한 물고기 미니게임에서 실행. 사각파 입력으로 바 물리(G,VMAX) 측정. ESC종료.", flush=True)

END_GRACE = 1.6         # 바가 이 시간 이상 연속으로 사라져야 '미니게임 종료'로 판단(flicker 방어)
MIN_SAMPLES = 320       # G 정밀측정 위해 여러 미니게임에 걸쳐 누적(한 판 부족하면 다음 판 계속 대기)
CONFIRM_FRAMES = 5      # (바+물고기) 가 이만큼 연속돼야 '진짜 미니게임'으로 확정 → 그 전엔 클릭 안 함

samples = []   # (t, bar_y, holding)
t0 = time.time()
last_flip = None
last_bar_t = None       # 마지막으로 바를 본 시각
holding = True
seen = False            # 진짜 미니게임 확정 & 측정 시작 여부
confirm = 0             # (바+물고기) 연속 프레임 수
last_report = 0
print("[calib] 대기 중... 직접 캐스팅→후킹해서 미니게임을 띄우세요. 진짜 미니게임(바+물고기)이", flush=True)
print("        확정되기 전엔 마우스를 건드리지 않습니다(자동 후킹 방지).", flush=True)
with mss.mss() as sct:
    while not quit["q"]:
        now = time.time()
        frame = grab(sct, cfg["roi"])
        bar, fish, _, _ = detect(frame, cfg)

        if not seen:
            # === 확정 전: 클릭 절대 안 함. 바 '그리고' 물고기가 함께 잡혀야 진짜 미니게임. ===
            if bar is not None and fish is not None:
                confirm += 1
                if confirm >= CONFIRM_FRAMES:
                    seen = True
                    last_flip = now
                    last_bar_t = now
                    print("[calib] 미니게임 확정! 사각파 측정 시작(물고기는 무시→곧 놓침=정상).", flush=True)
            else:
                confirm = 0
            el = time.time() - now
            if el < interval:
                time.sleep(interval - el)
            continue

        # === 측정 중 ===
        if bar is not None:
            last_bar_t = now
            if hwnd and ctypes.windll.user32.GetForegroundWindow() != hwnd:
                focus_window(hwnd)
            if now - last_flip >= HOLD_S:      # 사각파 토글
                holding = not holding
                last_flip = now
            mouse.set(holding, now, 0.0)
            samples.append((now - t0, float(bar["center"]), holding))
            if len(samples) - last_report >= 30:   # 진행 표시
                last_report = len(samples)
                ys_so_far = [s[1] for s in samples]
                print("[calib] 수집 %d (bar_y %.0f~%.0f)..."
                      % (len(samples), min(ys_so_far), max(ys_so_far)), flush=True)
        else:
            # 바 안 보임. grace 안이면 직전 동작 유지(끊김 방어), grace 넘으면 종료 판정.
            if last_bar_t is not None and (now - last_bar_t) > END_GRACE:
                if len(samples) >= MIN_SAMPLES:
                    break                       # 충분히 모음 → 진짜 종료
                # 부족 → 이번 판은 표본 부족. 마우스 놓고 다음 미니게임을 계속 기다림.
                mouse.up()
                print("[calib] 표본 %d개(부족). 한 판 더 하세요(캐스팅→후킹). 계속 대기 중..."
                      % len(samples), flush=True)
                seen = False
                confirm = 0
                last_bar_t = None
                # samples 는 유지(누적). 다음 판 표본과 합쳐 분석.
        el = time.time() - now
        if el < interval:
            time.sleep(interval - el)
mouse.up()
print("[calib] 수집 %d 프레임. 분석 중..." % len(samples), flush=True)

if len(samples) < 12:
    print("[calib] 표본 부족. 미니게임이 떠 있을 때 다시 실행하세요.", flush=True)
    raise SystemExit

# 원시 표본 저장(사후 검증용)
try:
    with open("calib_samples.csv", "w", encoding="utf-8") as f:
        f.write("t,bar_y,holding\n")
        for t, y, h in samples:
            f.write("%.4f,%.2f,%d\n" % (t, y, 1 if h else 0))
    print("[calib] 원시 표본 저장: calib_samples.csv", flush=True)
except Exception as e:
    print("[calib] CSV 저장 실패: %s" % e, flush=True)

# --- 프레임 속도 계산(px/s). 벽 근처(위/아래 끝 BAR_H/2+10) 표본은 반발 왜곡 → 제외 표시.
ts = np.array([s[0] for s in samples])
ys = np.array([s[1] for s in samples])
hs = np.array([s[2] for s in samples])

y_rng = float(ys.max() - ys.min())
print("[calib] bar_y 범위 %.0f~%.0f (진폭 %.0f px)" % (ys.min(), ys.max(), y_rng), flush=True)
if y_rng < 20:
    print("[calib] !! 바가 거의 안 움직임 → 마우스 입력이 게임에 안 먹혔거나 정적 초록 오검출.", flush=True)
    print("[calib]    게임을 포그라운드(전체화면)로 두고, 진짜 미니게임에서 다시 실행하세요.", flush=True)
margin = BAR_H / 2 + 10.0
near_wall = (ys < margin) | (ys > track_h - margin)
dt_med = float(np.median(np.diff(ts))) if len(ts) > 2 else 1.0 / 60.0


def analyze(want_hold):
    """want_hold 구간들에서 '가속도'를 위치 2차피팅으로 뽑는다(속도 미분보다 노이즈에 강함).
    각 holding 구간을 프레임드롭(dt>2.5*중앙값)으로 쪼갠 뒤, 벽에 안 닿고(near_wall X)
    충분히 긴(≥8프레임) & 실제로 움직인(≥25px) 클린 서브구간에서 y=a t^2+b t+c 피팅 → G=|2a|.
    또 그 서브구간의 최대 |창속도|(0.1s)를 모아 VMAX(하한)로 쓴다."""
    accs, peakv = [], []
    W = max(4, int(round(0.1 / max(dt_med, 1e-3))))
    i, n = 0, len(samples)
    while i < n:
        if hs[i] != want_hold:
            i += 1
            continue
        j = i
        while j < n and hs[j] == want_hold:
            j += 1
        idx = list(range(i, j))
        # 프레임드롭으로 서브구간 분할
        runs, cur = [], [idx[0]] if idx else []
        for k in range(1, len(idx)):
            if ts[idx[k]] - ts[idx[k - 1]] > 2.5 * dt_med:
                runs.append(cur); cur = []
            cur.append(idx[k])
        if cur:
            runs.append(cur)
        for r in runs:
            r = [k for k in r if not near_wall[k]]      # 벽 프레임 제외
            if len(r) < 8:
                continue
            tt = ts[r] if isinstance(r, np.ndarray) else np.array([ts[k] for k in r])
            yy = np.array([ys[k] for k in r])
            tt = tt - tt[0]
            if abs(yy[-1] - yy[0]) < 25:                # 거의 안 움직인 서브구간 제외(벽대기 등)
                continue
            a2 = np.polyfit(tt, yy, 2)[0]
            accs.append(abs(2.0 * a2))
            # 창속도 최대(VMAX 하한)
            best = 0.0
            for k in range(len(r) - W):
                dtt = tt[k + W] - tt[k]
                if dtt > 1e-3:
                    best = max(best, abs((yy[k + W] - yy[k]) / dtt))
            if best > 0:
                peakv.append(best)
        i = j
    g = float(np.median(accs)) if accs else float("nan")
    vmax = float(np.percentile(peakv, 90)) if peakv else float("nan")
    return g, vmax, len(accs)


g_up, vmax_up, n_up = analyze(True)      # 누름=위
g_dn, vmax_dn, n_dn = analyze(False)     # 뗌=아래
VMAX = float(np.nanmax([vmax_up, vmax_dn]))
G = float(np.nanmedian([g_up, g_dn]))
print("[calib] 누름(위)  G=%.0f px/s^2   관측최대속도=%.0f px/s  (클린램프 %d)"
      % (g_up, vmax_up, n_up), flush=True)
print("[calib] 뗌(아래)  G=%.0f px/s^2   관측최대속도=%.0f px/s  (클린램프 %d)"
      % (g_dn, vmax_dn, n_dn), flush=True)
print("[calib] === 추정 물리(SDV 대칭 모델) ===", flush=True)
print("[calib]   G    ~ %.0f  px/s^2" % G, flush=True)
print("[calib]   VMAX ~ %.0f  px/s" % VMAX, flush=True)
print("[calib] sim_tune.py 의 PHYS_ANCHORS 를 아래로 교체 후 재실행:", flush=True)
print("        PHYS_ANCHORS = [(%.0f, %.0f)]" % (G, VMAX), flush=True)
print("[calib] (누름/뗌 VMAX·G 가 크게 다르면 비대칭 → sim_bar 를 방향별 G 로 확장 고려)", flush=True)
