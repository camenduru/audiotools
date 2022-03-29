import copy

import julius
import numpy as np
import pyloudnorm
import scipy
import torch
import torch.nn.functional as F
import torchaudio

from . import util


def unfold1d(input, kernel_size: int, stride: int):
    """Fast version of unfold 1d. Taken from:
    https://github.com/pytorch/pytorch/issues/60466
    """
    *shape, length = input.shape
    n_frames = (max(length, kernel_size) - kernel_size) // stride + 1
    tgt_length = (n_frames - 1) * stride + kernel_size
    input = input[..., :tgt_length].contiguous()
    strides = list(input.stride())
    strides = strides[:-1] + [stride, 1]
    out = input.as_strided(shape + [n_frames, kernel_size], strides)
    return out.transpose(-1, -2)


class Meter(torch.nn.Module, pyloudnorm.Meter):
    """Tensorized version of pyloudnorm.Meter. Works with batched audio tensors."""

    def __init__(
        self,
        rate,
        filter_class="K-weighting",
        block_size=0.400,
        zeros=512,
        use_fir=False,
    ):
        super().__init__()

        self.rate = rate
        self.filter_class = filter_class
        self.block_size = block_size
        self.use_fir = use_fir

        G = torch.from_numpy(np.array([1.0, 1.0, 1.0, 1.41, 1.41]))
        self.register_buffer("G", G)

        # Compute impulse responses so that filtering is fast via
        # a convolution at runtime, on GPU, unlike lfilter.
        impulse = np.zeros((zeros,))
        impulse[..., 0] = 1.0

        firs = np.zeros((len(self._filters), 1, zeros))
        passband_gain = torch.zeros(len(self._filters))

        for i, (_, filter_stage) in enumerate(self._filters.items()):
            firs[i] = scipy.signal.lfilter(filter_stage.b, filter_stage.a, impulse)
            passband_gain[i] = filter_stage.passband_gain

        firs = torch.from_numpy(firs[..., ::-1].copy()).float()

        self.register_buffer("firs", firs)
        self.register_buffer("passband_gain", passband_gain)

    def apply_filter_gpu(self, data):
        # Data is of shape (nb, nch, nt)
        # Reshape to (nb*nch, 1, nt)
        nb, nt, nch = data.shape
        data = data.permute(0, 2, 1)
        data = data.reshape(nb * nch, 1, nt)

        # Apply padding
        pad_length = self.firs.shape[-1]

        # Apply filtering in sequence
        for i in range(self.firs.shape[0]):
            data = F.pad(data, (pad_length, pad_length))
            data = julius.fftconv.fft_conv1d(data, self.firs[i, None, ...])
            data = self.passband_gain[i] * data
            data = data[..., 1 : nt + 1]

        data = data.permute(0, 2, 1)
        data = data[:, :nt, :]
        return data

    def apply_filter_cpu(self, data):
        for _, filter_stage in self._filters.items():
            passband_gain = filter_stage.passband_gain

            a_coeffs = torch.from_numpy(filter_stage.a).float().to(data.device)
            b_coeffs = torch.from_numpy(filter_stage.b).float().to(data.device)

            _data = data.permute(0, 2, 1)
            filtered = torchaudio.functional.lfilter(
                _data, a_coeffs, b_coeffs, clamp=False
            )
            data = passband_gain * filtered.permute(0, 2, 1)
        return data

    def apply_filter(self, data):
        if data.is_cuda or self.use_fir:
            data = self.apply_filter_gpu(data)
        else:
            data = self.apply_filter_cpu(data)
        return data

    def forward(self, data):
        return self.integrated_loudness(data)

    def unfold(self, input_data):
        T_g = self.block_size
        overlap = 0.75  # overlap of 75% of the block duration
        step = 1.0 - overlap  # step size by percentage

        kernel_size = int(T_g * self.rate)
        stride = int(T_g * self.rate * step)
        unfolded = unfold1d(input_data.permute(0, 2, 1), kernel_size, stride)

        return unfolded

    def integrated_loudness(self, data):
        if not torch.is_tensor(data):
            data = torch.from_numpy(data).float()
        else:
            data = data.float()

        input_data = copy.copy(data)
        # Data always has a batch and channel dimension.
        # Is of shape (nb, nt, nch)
        if input_data.ndim < 2:
            input_data = input_data.unsqueeze(-1)
        if input_data.ndim < 3:
            input_data = input_data.unsqueeze(0)

        nb, nt, nch = input_data.shape

        # Apply frequency weighting filters - account
        # for the acoustic respose of the head and auditory system
        input_data = self.apply_filter(input_data)

        G = self.G  # channel gains
        T_g = self.block_size  # 400 ms gating block standard
        Gamma_a = -70.0  # -70 LKFS = absolute loudness threshold

        unfolded = self.unfold(input_data)

        z = (1.0 / (T_g * self.rate)) * unfolded.square().sum(2)
        l = -0.691 + 10.0 * torch.log10((G[None, :nch, None] * z).sum(1, keepdim=True))
        l = l.expand_as(z)

        # find gating block indices above absolute threshold
        z_avg_gated = z
        z_avg_gated[l <= Gamma_a] = 0
        masked = l > Gamma_a
        z_avg_gated = z_avg_gated.sum(2) / masked.sum(2)

        # calculate the relative threshold value (see eq. 6)
        Gamma_r = (
            -0.691 + 10.0 * torch.log10((z_avg_gated * G[None, :nch]).sum(-1)) - 10.0
        )
        Gamma_r = Gamma_r[:, None, None]
        Gamma_r = Gamma_r.expand(nb, nch, l.shape[-1])

        # find gating block indices above relative and absolute thresholds  (end of eq. 7)
        z_avg_gated = z
        z_avg_gated[l <= Gamma_a] = 0
        z_avg_gated[l <= Gamma_r] = 0
        masked = (l > Gamma_a) * (l > Gamma_r)
        z_avg_gated = z_avg_gated.sum(2) / masked.sum(2)

        # # Cannot use nan_to_num (pytorch 1.8 does not come with GCP-supported cuda version)
        # z_avg_gated = torch.nan_to_num(z_avg_gated)
        z_avg_gated = torch.where(
            z_avg_gated.isnan(), torch.zeros_like(z_avg_gated), z_avg_gated
        )
        z_avg_gated[z_avg_gated == float("inf")] = float(np.finfo(np.float32).max)
        z_avg_gated[z_avg_gated == -float("inf")] = float(np.finfo(np.float32).min)

        LUFS = -0.691 + 10.0 * torch.log10((G[None, :nch] * z_avg_gated).sum(1))
        return LUFS.float()


class LoudnessMixin:
    _loudness = None
    MIN_LOUDNESS = -70

    def loudness(self, filter_class="K-weighting", block_size=0.400, **kwargs):
        """
        Uses pyloudnorm to calculate loudness.
        Implementation of ITU-R BS.1770-4.
        Allows control over gating block size and frequency weighting filters for
        additional control.
        Measure the integrated gated loudness of a signal.

        Uses the weighting filters and block size defined by the meter
        the integrated loudness is measured based upon the gating algorithm
        defined in the ITU-R BS.1770-4 specification.
        Supports up to 5 channels and follows the channel ordering:
        [Left, Right, Center, Left surround, Right surround]
        Args:
            filter_class (str):
              Class of weighting filter used.
              - 'K-weighting' (default)
              - 'Fenton/Lee 1'
              - 'Fenton/Lee 2'
              - 'Dash et al.'
            block_size (float):
              Gating block size in seconds. Defaults to 0.400.
        Returns:
            float: LUFS, Integrated gated loudness of the input
              measured in dB LUFS.
        """
        if self._loudness is not None:
            return self._loudness.to(self.device)
        original_length = self.signal_length
        if self.signal_duration < 0.5:
            pad_len = int((0.5 - self.signal_duration) * self.sample_rate)
            self.zero_pad(0, pad_len)

        # create BS.1770 meter
        meter = Meter(
            self.sample_rate, filter_class=filter_class, block_size=block_size, **kwargs
        )
        meter = meter.to(self.device)
        # measure loudness
        loudness = meter.integrated_loudness(self.audio_data.permute(0, 2, 1))
        self.truncate_samples(original_length)
        min_loudness = (
            torch.ones_like(loudness, device=loudness.device) * self.MIN_LOUDNESS
        )
        self._loudness = torch.maximum(loudness, min_loudness)

        return self._loudness.to(self.device)
