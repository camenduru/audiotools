import math
from inspect import signature
from readline import parse_and_bind
from typing import List

import torch
from numpy.random import RandomState

from ..core import AudioSignal
from ..core import util

tt = torch.tensor


class BaseTransform:
    def __init__(self, keys: list = [], prob: float = 1.0):
        self.keys = keys + ["signal"]
        self.prob = prob

    def validate(self, batch: dict):
        for k in self.keys:
            assert k in batch.keys(), f"{k} not in batch"

        if "original" not in batch:
            batch["original"] = batch["signal"].copy()
        batch["copy"] = batch["signal"].copy()

        return batch

    def apply_mask(self, batch, copy_key="copy"):
        mask_key = f"{self.__class__.__name__}.mask"
        if mask_key not in batch:
            return batch
        mask = batch[mask_key]
        copy = batch[copy_key]
        batch["signal"].audio_data[mask] = copy.audio_data[mask]
        return batch

    def _transform(self, batch: dict):
        raise NotImplementedError

    def _instantiate(self, state: RandomState, signal: AudioSignal = None):
        raise NotImplementedError

    def transform(self, batch: dict):
        batch = self.validate(batch)
        batch = self._transform(batch)
        batch = self.apply_mask(batch)
        return batch

    def __call__(self, batch: dict):
        return self.transform(batch)

    def instantiate(self, state: RandomState, signal: AudioSignal = None):
        state = util.random_state(state)
        params = self._instantiate(state, signal)
        mask = state.rand() <= (1 - self.prob)
        params.update({f"{self.__class__.__name__}.mask": mask})

        for k, v in params.items():
            if not torch.is_tensor(v):
                params[k] = tt(v)

        return params


class Compose(BaseTransform):
    def __init__(self, transforms: list, prob: float = 1.0):
        super().__init__(prob=prob)

        self.transforms = transforms

    def transform(self, batch: dict):
        batch = self.validate(batch)
        batch["compose_copy"] = batch["signal"].copy()
        for transform in self.transforms:
            batch = transform(batch)
        batch = self.apply_mask(batch, "compose_copy")
        return batch

    def _instantiate(self, state: RandomState, signal: AudioSignal = None):
        parameters = {}
        for transform in self.transforms:
            parameters.update(transform.instantiate(state, signal=signal))
        return parameters


class ClippingDistortion(BaseTransform):
    def __init__(self, clip_range: list = [0.0, 0.1], prob: float = 1.0):
        keys = ["clip_percentile"]
        super().__init__(keys=keys, prob=prob)

        self.clip_range = clip_range

    def _instantiate(self, state: RandomState, signal: AudioSignal = None):
        return {
            "clip_percentile": state.uniform(self.clip_range[0], self.clip_range[1])
        }

    def _transform(self, batch):
        signal = batch["signal"]
        clip_percentile = batch["clip_percentile"]
        batch["signal"] = signal.clip_distortion(clip_percentile)
        return batch


class Equalizer(BaseTransform):
    def __init__(self, eq_amount: float = 1.0, n_bands: int = 6, prob: float = 1.0):
        super().__init__(prob=prob)

        self.eq_amount = eq_amount
        self.n_bands = n_bands

    def _instantiate(self, state: RandomState, signal: AudioSignal = None):
        eq_curve = -self.eq_amount * state.rand(self.n_bands)
        return {"eq_curve": eq_curve}

    def _transform(self, batch):
        signal = batch["signal"]
        eq_curve = batch["eq_curve"]
        batch["signal"] = signal.equalizer(eq_curve)
        return batch