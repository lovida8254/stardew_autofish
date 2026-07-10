"""미니게임 중 바 검출 안정성 진단. 매 프레임: bar_mask 총 px, 초록 덩어리들(area,y,h),
detect()가 바를 잡는지. 바가 왜 끊기는지 파악. 사용자가 후킹해서 미니게임 띄우면 됨."""
import time
import numpy as np, mss, cv2
from main import load_config, grab, detect

cfg = load_config()
blo = np.array(cfg["bar_hsv_lower"]); bhi = np.array(cfg["bar_hsv_upper"])
R = cfg["roi"]; bmin = int(cfg["bar_min_area"])

with mss.mss() as sct:
    print("[barstab] 미니게임 대기. 후킹해서 띄우세요. 바 검출 안정성 로깅. 80s.", flush=True)
    t0 = time.time(); last = 0.0; active = False; nlog = 0
    while time.time() - t0 < 80.0 and nlog < 120:
        t = time.time() - t0
        frame = grab(sct, R)
        bar, fish, bm, fm = detect(frame, cfg)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        gmask = cv2.inRange(hsv, blo, bhi)
        gpx = int((gmask > 0).sum())
        cnts = cv2.findContours(gmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
        blobs = sorted([(cv2.contourArea(c),) + cv2.boundingRect(c) for c in cnts], reverse=True)[:3]
        # 미니게임 활성 판단: 초록 px가 좀 있으면
        if gpx > 200:
            active = True
        if active and t - last > 0.12:
            bs = " ".join("[a=%d y=%d h=%d]" % (a, y, h) for a, x, y, w, h in blobs)
            print("[barstab] t=%5.2f bar=%s gpx=%d min=%d 초록:%s" % (
                t, ("y%d" % int(bar["center"]) if bar else "NONE"), gpx, bmin, bs), flush=True)
            last = t; nlog += 1
        elif not active and t - last > 4.0:
            print("[barstab] t=%5.2f 대기(gpx=%d)..." % (t, gpx), flush=True); last = t
        time.sleep(0.03)
print("[barstab] === done ===", flush=True)
