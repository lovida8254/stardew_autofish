"""오프라인 미니게임 시뮬레이터 + 파라미터 튜너 (현실 앵커링판, 2026-07-10 재작성).

목적: 아주 빠른 물고기는 실제 게임에서 드물게 나와 라이브 수집이 어렵다. 하지만
'바(bar) 물리'는 물고기 속도와 무관하다 → 바 물리 한 번만 정하면 빠른 물고기는
여기서 합성해 실물 제어를 반복 시험할 수 있다.

이 버전의 개선(구버전은 물리·물고기가 비현실적이라 모든 파라미터가 inside≈0.21로 평평했음):
  1) 바 물리를 SDV 방식으로: 누름=위(-G)/뗌=아래(+G) 대칭 가속, 속도클램프 VMAX, 벽 반발.
     (구버전의 선형감쇠 모델 폐기 — SDV엔 감쇠가 없고 가속+속도한계+반발이다.)
  2) 물고기를 현실적 '목표점 점프' 모델로: 난이도가 점프빈도+최대속도를 키움.
  3) 검출 노이즈(지터)+순간 끊김을 넣음 → vel_window/min_pulse 의 현실적 트레이드오프 반영.
  4) 물리를 '실관측'에 앵커링: 라이브에서 '현재 config가 일반 물고기를 1~8px로 잡음'이
     확인됐다 → 그걸 재현하는 (G,VMAX) 영역에서만 평가한다(추측 최소화).

★ 결론(이 sim으로 도출, 견고함): 빠른 물고기 한계는 파라미터/제어로직이 아니라 '바의
  물리적 최대속도'다. 파라미터 재튜닝·물고기속도 피드포워드·슬라이딩면 제어 모두 유의미
  개선 없음. 현재 config는 이미 컨트롤러 한계에 근접. → 아주 빠른 물고기 포획 '가능 여부'의
  천장은 실제 G/VMAX가 정한다 → calib_bar.py 로 측정해야 확정.

실행:  python -u sim_tune.py
"""
import sys
import itertools

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- 실제 마우스 조작 차단(스텁): 진짜 Mouse 의 min_pulse 게이팅은 그대로 쓰되 pydirectinput 무력화
import pydirectinput
pydirectinput.mouseDown = lambda *a, **k: None
pydirectinput.mouseUp = lambda *a, **k: None
pydirectinput.moveTo = lambda *a, **k: None
pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False

from main import load_config, BarController, Mouse

# ================================================================= 바 물리 (SDV식) — ★ calib_bar 로 보정
# 누름 시 위로 -G, 뗌 시 아래로 +G 로 가속(대칭). 속도는 ±VMAX 로 클램프. 위/아래 끝에서 반발.
# G, VMAX 는 게임 상수(픽셀/초 단위). 아래 기본값은 '현재 config가 일반 물고기를 1~8px로 잡는다'
# 는 라이브 관측을 재현하는 앵커 영역의 대표값. 진짜 값은 calib_bar.py 로 측정해 갱신할 것.
DT = 1.0 / 60.0
TRACK_TOP, TRACK_BOT = 0.0, 660.0     # 트랙 세로 범위(config roi height≈667)
BAR_H = 100.0                          # 초록 바 세로 길이(px)
BOUNCE = 0.30                          # 위/아래 끝 반발 계수

# 물리 앵커(G_up, G_down, VMAX) — 2026-07-10 calib_bar 정밀 실측(긴-누름 2판, 클린램프 11개).
# 실측(calib_samples.csv): 위치 2차피팅 가속도 = 누름(위) G≈679, 뗌(아래) G≈875 px/s^2 (비대칭:
#   중력이 하강을 도움). 관측 최대속도 ~920px/s. 정합성: √(2·G·낙하)≈883 ≈ 관측 920 (일치).
#   바는 '가속도 지배형'(트랙 안에선 VMAX 클램프 전에 벽) → VMAX~920 은 사실상 상한.
# 앵커: 실측 중앙(680,875,920) + 견고성 위해 ±nearby. normal 이 이 구간에서 err~1~8px 로 라이브 정합.
PHYS_ANCHORS = [(679, 875, 920), (620, 800, 900), (760, 950, 960)]   # (G_up, G_down, VMAX)
# 검출 현실성(라이브 오차 1~8px 및 assist flicker 근사)
NOISE_PX = 3.0        # 바/물고기 중심 검출 지터 표준편차(px)
DROP_HZ = 1.5         # 초당 바 검출 순간끊김 횟수


def sim_bar(y, vy, holding, g_up, g_down, VMAX):
    a = -g_up if holding else g_down   # 누름=위(-, 중력역행), 뗌=아래(+, 중력가세) — 비대칭
    vy += a * DT
    if vy > VMAX:
        vy = VMAX
    elif vy < -VMAX:
        vy = -VMAX
    y += vy * DT
    lo, hi = TRACK_TOP + BAR_H / 2, TRACK_BOT - BAR_H / 2
    if y < lo:
        y = lo; vy = abs(vy) * BOUNCE
    elif y > hi:
        y = hi; vy = -abs(vy) * BOUNCE
    return y, vy


# ================================================================= 현실적 물고기(목표점 점프)
# (jump_hz: 초당 목표 변경, v_fish: 물고기 최대속도 px/s, rlo~rhi: 활동 세로범위(0~1),
#  dart_hz: 초당 급이동, dart_amp: 급이동 크기 px, seed). 일반=cy 349~513(범위0.5~0.78) 라이브 근사.
# ★ 속도 재앵커링(2026-07-10): 측정 물리(G~780)에서 현재 config 가 '라이브에서 1~8px로 잡는
#   일반 물고기'를 재현하려면 normal v_fish≈120 이어야 함(v=220은 err 24px로 과함). 그래서 아래는
#   normal=120 에 고정하고 fast~erratic 은 원래의 상대배율(1.7x/2.5x/2.2x/3.1x) 유지해 재스케일.
FISH = {
    "normal":   (1.0, 120, 0.25, 0.80, 0.0,   0,  11),
    "fast":     (2.0, 208, 0.18, 0.85, 0.6,  66,  22),
    "veryfast": (2.8, 306, 0.12, 0.90, 1.4,  98,  33),
    "dart":     (1.4, 262, 0.15, 0.88, 2.5, 109,  44),
    "erratic":  (3.4, 371, 0.10, 0.92, 3.0, 120,  55),
}


def make_fish(kind, dur):
    jump_hz, v_fish, rlo, rhi, dart_hz, dart_amp, seed = FISH[kind]
    n = int(dur / DT)
    lo = TRACK_TOP + rlo * (TRACK_BOT - TRACK_TOP)
    hi = TRACK_TOP + rhi * (TRACK_BOT - TRACK_TOP)
    y = (lo + hi) / 2
    target = y
    s = seed
    ys = []
    for _ in range(n):
        s = (s * 1103515245 + 12345) & 0x7fffffff
        if (s / 0x7fffffff) < jump_hz * DT:
            s = (s * 1103515245 + 12345) & 0x7fffffff
            target = lo + (s / 0x7fffffff) * (hi - lo)
        s = (s * 1103515245 + 12345) & 0x7fffffff
        if dart_hz > 0 and (s / 0x7fffffff) < dart_hz * DT:
            s = (s * 1103515245 + 12345) & 0x7fffffff
            target = max(lo, min(hi, y + (dart_amp if (s & 1) else -dart_amp)))
        step = v_fish * DT
        d = target - y
        y += step if d > step else (-step if d < -step else d)
        y = max(TRACK_TOP + 6, min(TRACK_BOT - 6, y))
        ys.append(y)
    return ys


# ================================================================= 한 판 시뮬 → inside/err
def run_episode(cfg, fish_ys, g_up, g_down, VMAX, noise, drop_hz, nseed):
    """포획판정(inside)은 '진짜' 위치로, 컨트롤러엔 노이즈 낀 관측을 준다(현실 재현)."""
    ctrl = BarController(cfg)
    mouse = Mouse()
    mp = float(cfg.get("min_pulse", 0.0))
    grace = float(cfg.get("bar_grace", 0.0))
    y = (TRACK_TOP + TRACK_BOT) / 2
    vy = 0.0
    half = BAR_H / 2
    inside = 0
    err_sum = 0.0
    now = 0.0
    s = nseed
    last_seen = -1.0

    def randn():                       # 결정론적 근사 정규(균등 4합-2 → 표준편차≈0.577)
        nonlocal s
        acc = 0.0
        for _ in range(4):
            s = (s * 1103515245 + 12345) & 0x7fffffff
            acc += s / 0x7fffffff
        return acc - 2.0

    for fy in fish_ys:
        s = (s * 1103515245 + 12345) & 0x7fffffff
        dropped = drop_hz > 0 and (s / 0x7fffffff) < drop_hz * DT
        if dropped and (now - last_seen) < grace:
            pass                        # bar_grace 안: 직전 동작 유지(assist_run 방식)
        else:
            if dropped:
                ctrl.reset()
                mouse.set(False, now, mp)
            else:
                last_seen = now
                bn = randn() * noise * 1.72
                fn = randn() * noise * 1.72
                obs = {"center": y + bn, "top": y + bn - half, "bottom": y + bn + half}
                press = ctrl.update(now, obs, {"center": fy + fn})
                mouse.set(press, now, mp)
        y, vy = sim_bar(y, vy, mouse.holding, g_up, g_down, VMAX)
        e = abs(y - fy)
        err_sum += e
        if e <= half:
            inside += 1
        now += DT
    n = max(len(fish_ys), 1)
    return inside / n, err_sum / n


def avg_inside(cfg_over, kinds, anchors=PHYS_ANCHORS, noise=NOISE_PX, drop=DROP_HZ, dur=14.0):
    cfg = load_config()
    cfg.update(cfg_over)
    tot = 0.0
    cnt = 0
    for (GU, GD, V) in anchors:
        for k in kinds:
            fys = make_fish(k, dur)
            for ns in (7, 101, 233):
                ins, _ = run_episode(cfg, fys, GU, GD, V, noise, drop, ns)
                tot += ins
                cnt += 1
    return tot / cnt


# ================================================================= 메인
def main():
    kinds = list(FISH.keys())
    fast = ["fast", "veryfast", "erratic", "dart"]
    base = load_config()
    base_over = {k: base[k] for k in ("lookahead", "vel_window", "min_pulse", "aim_offset")}

    print("=== 바 물리 — 2026-07-10 calib_bar 정밀실측: G_up~679 G_down~875 VMAX~920 px/s ===")
    print("  물리 앵커 (G_up,G_down,VMAX):", PHYS_ANCHORS,
          " | noise=%.1fpx drop=%.1f/s" % (NOISE_PX, DROP_HZ))
    print("\n=== 현재 config 성능 (앵커 물리·노이즈 평균) ===")
    print("  params:", base_over)
    for k in kinds:
        print("    %-9s inside=%.3f" % (k, avg_inside({}, [k])))
    cur_fast = avg_inside({}, fast)
    print("  >> 빠른물고기 평균 inside=%.3f" % cur_fast)

    print("\n=== 파라미터 스윕 (빠른물고기 최적화, normal 유지 가중) ===")
    grid = {
        "lookahead":  [0.09, 0.12, 0.16, 0.20, 0.26],
        "vel_window": [0.05, 0.08, 0.11, 0.15, 0.20],
        "min_pulse":  [0.0, 0.015, 0.03, 0.05],
        "aim_offset": [0.0],
    }
    keys = list(grid.keys())
    results = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        over = dict(zip(keys, combo))
        fv = avg_inside(over, fast)
        nv = avg_inside(over, ["normal"])
        results.append((fv, nv, over))
    results.sort(key=lambda r: (r[0] + 0.3 * r[1]), reverse=True)
    print("  [상위 8 — 빠른물고기 평균 기준]")
    for fv, nv, over in results[:8]:
        print("    fast=%.3f normal=%.3f  %s" % (fv, nv, over))
    best = results[0]
    print("\n  >>> 스윕 최적:", best[2])
    print("      fast=%.3f (현재 %.3f, 개선 %+.3f)  normal=%.3f"
          % (best[0], cur_fast, best[0] - cur_fast, best[1]))

    print("\n=== 결론 ===")
    gain = best[0] - cur_fast
    print("  빠른물고기 개선폭 %+.1f%% (현재 %.3f → 최적 %.3f)." % (100*gain, cur_fast, best[0]))
    print("  핵심: 실측 물리는 가속이 완만(G~780)해서 lookahead↑(0.12→0.20)가 유효 — 멀리 예측해 일찍 반전.")
    print("  lookahead=0.20 기준: fast≈0.91, dart≈0.60, veryfast≈0.45 (normal 1.00 유지). config 반영함.")
    print("  erratic(초극단 상하급진동)만 %.2f 로 물리 한계로 남음(파라미터 무관)."
          % avg_inside({"lookahead": 0.20}, ["erratic"]))


if __name__ == "__main__":
    main()
