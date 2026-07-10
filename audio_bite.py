"""
입질 소리 감지 (WASAPI 루프백)
================================
스피커로 나가는 게임 소리를 그대로 캡처해서, 조용한 대기 상태에서 갑자기
튀는 소리(입질음 "똑")를 순간 에너지 스파이크로 감지한다.

- 배경 스레드가 계속 짧은 블록을 녹음하며 RMS(음량)와 완만한 배경 기준선을 갱신
- arm()으로 켠 동안만 스파이크를 기록 (캐스팅/미니게임 소리 오탐 방지)
- consume_spike()로 스파이크 발생 여부를 1회성으로 가져감
"""

import time
import threading

import numpy as np


def is_spike(rms, baseline, floor, ratio):
    """순간 음량이 절대 바닥값과 배경 기준선 배수를 동시에 넘으면 스파이크."""
    return rms > floor and rms > baseline * ratio


class AudioBiteDetector:
    def __init__(self, cfg, log=lambda m: None):
        self.cfg = cfg
        self.log = log
        self.level = 0.0      # 최근 블록 RMS (UI 표시용)
        self.baseline = 0.0   # 완만한 배경 기준선
        self.ok = False       # 캡처 정상 여부
        self._spike = False
        self._last_spike_t = 0.0
        self._armed = False
        self._stop = False
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def arm(self, on=True):
        """대기 상태 동안만 True. 켤 때 남은 스파이크 플래그를 비운다."""
        self._armed = on
        if on:
            with self._lock:
                self._spike = False

    def consume_spike(self):
        with self._lock:
            s = self._spike
            self._spike = False
        return s

    def stop(self):
        self._stop = True

    def _run(self):
        try:
            import soundcard as sc
        except Exception as e:
            self.log(f"오디오 라이브러리(soundcard) 없음: {e}")
            return

        sr, block = 48000, 1024  # 블록당 약 21ms
        while not self._stop:
            try:
                spk = sc.default_speaker()
                mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
                with mic.recorder(samplerate=sr, channels=1, blocksize=block) as rec:
                    self.ok = True
                    self.log(f"입질 소리 감지 시작: {spk.name}")
                    ema = None
                    while not self._stop:
                        data = rec.record(numframes=block)
                        rms = float(np.sqrt(np.mean(np.square(data.astype(np.float64)))) + 1e-9)
                        self.level = rms
                        # 배경 기준선: 완만한 EMA. 스파이크가 기준선을 급히 끌어올리지 않도록 클램프
                        if ema is None:
                            ema = rms
                        a = 0.03
                        ema = (1 - a) * ema + a * min(rms, ema * 1.5)
                        self.baseline = ema

                        ratio = float(self.cfg.get("bite_sound_ratio", 3.5))
                        floor = float(self.cfg.get("bite_sound_floor", 0.03))
                        refr = float(self.cfg.get("bite_refractory", 1.0))
                        now = time.time()
                        if (self._armed and is_spike(rms, ema, floor, ratio)
                                and (now - self._last_spike_t) > refr):
                            self._last_spike_t = now
                            with self._lock:
                                self._spike = True
            except Exception as e:
                self.ok = False
                self.log(f"오디오 캡처 오류(재시도): {e}")
                time.sleep(1.0)
