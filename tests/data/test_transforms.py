import json
from pathlib import Path

import pytest
import torch
from numpy.random import RandomState

import audiotools
from audiotools import AudioSignal
from audiotools import util
from audiotools.data import transforms as tfm

transforms_to_test = []
for x in dir(tfm):
    if hasattr(getattr(tfm, x), "transform"):
        if x not in ["Compose"]:
            transforms_to_test.append(x)


def _compare_transform(transform_name, signal):
    regression_data = Path(f"tests/regression/transforms/{transform_name}.wav")
    regression_data.parent.mkdir(exist_ok=True, parents=True)

    if regression_data.exists():
        regression_signal = AudioSignal(regression_data)
        assert signal == regression_signal
    else:
        signal.write(regression_data)


@pytest.mark.parametrize("transform_name", transforms_to_test)
def test_transform(transform_name):
    seed = 0
    transform_cls = getattr(tfm, transform_name)

    kwargs = {}
    if transform_name == "BackgroundNoise":
        kwargs["csv_files"] = ["tests/audio/noises.csv"]
    if transform_name == "RoomImpulseResponse":
        kwargs["csv_files"] = ["tests/audio/irs.csv"]

    audio_path = "tests/audio/spk/f10_script4_produced.wav"
    signal = AudioSignal(audio_path, offset=10, duration=2)
    transform = transform_cls(prob=1.0, **kwargs)

    batch = transform.instantiate(seed, signal)
    if transform_name == "VolumeNorm":
        batch["VolumeNorm.loudness"] = AudioSignal(audio_path).ffmpeg_loudness().item()

    batch["signal"] = signal
    batch = transform(batch)

    output = batch["signal"]

    _compare_transform(transform_name, output)


def test_compose():
    seed = 0

    audio_path = "tests/audio/spk/f10_script4_produced.wav"
    signal = AudioSignal(audio_path, offset=10, duration=2)
    transform = tfm.Compose(
        [
            tfm.RoomImpulseResponse(csv_files=["tests/audio/irs.csv"]),
            tfm.BackgroundNoise(csv_files=["tests/audio/noises.csv"]),
        ]
    )

    batch = transform.instantiate(seed, signal)

    batch["signal"] = signal.clone()
    batch = transform(batch)
    output = batch["signal"]

    _compare_transform("Compose", output)


def test_masking():
    class DummyData(torch.utils.data.Dataset):
        def __init__(self, audio_path):
            super().__init__()

            self.audio_path = audio_path
            self.length = 100
            self.transform = tfm.Silence(prob=0.5)

        def __getitem__(self, idx):
            state = util.random_state(idx)
            signal = AudioSignal.salient_excerpt(
                self.audio_path, state=state, duration=1.0
            ).resample(44100)

            item = self.transform.instantiate(state, signal=signal)
            item["signal"] = signal

            return item

        def __len__(self):
            return self.length

    dataset = DummyData("tests/audio/spk/f10_script4_produced.wav")
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=16,
        num_workers=0,
        collate_fn=audiotools.data.datasets.collate,
    )
    for batch in dataloader:
        batch = dataset.transform(batch)
        mask = batch["Silence.mask"]

        zeros = torch.zeros_like(batch["signal"][mask].audio_data)
        original = batch["original"][~mask].audio_data

        assert torch.allclose(batch["signal"][mask].audio_data, zeros)
        assert torch.allclose(batch["signal"][~mask].audio_data, original)
