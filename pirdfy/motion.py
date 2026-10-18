"""Cheap motion gate so the detector only runs when something changes."""

from __future__ import annotations

import numpy as np

from .config import MotionSettings

_SIZE = (160, 90)  # analysis resolution (w, h); enough to see a bird land


class MotionGate:
    def __init__(self, settings: MotionSettings):
        self.settings = settings
        self._background: np.ndarray | None = None
        self.last_score = 0.0

    def reset(self) -> None:
        self._background = None

    def score(self, gray: np.ndarray) -> float:
        """Fraction of pixels that changed versus a slowly adapting background."""
        import cv2

        small = cv2.resize(gray, _SIZE, interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (5, 5), 0).astype(np.float32)
        if self._background is None or self._background.shape != small.shape:
            self._background = small
            self.last_score = 0.0
            return 0.0
        diff = cv2.absdiff(small, self._background)
        changed = float(np.count_nonzero(diff > self.settings.pixel_threshold)) / diff.size
        # Adapt to slow light changes (clouds, sunset) without absorbing a bird instantly.
        cv2.accumulateWeighted(small, self._background, 0.05)
        self.last_score = changed
        return changed

    def triggered(self, gray: np.ndarray) -> bool:
        if not self.settings.enabled:
            return True
        return self.score(gray) >= self.settings.area_threshold
