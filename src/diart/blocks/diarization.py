from typing import Optional, Any, Union, Tuple, Sequence

import numpy as np
import torch
from pyannote.core import Annotation, SlidingWindowFeature, SlidingWindow
from typing_extensions import Literal

from .aggregation import DelayedAggregation
from .clustering import OnlineSpeakerClustering
from .embedding import OverlapAwareSpeakerEmbedding
from .segmentation import SpeakerSegmentation
from .utils import Binarize
from .. import models as m


class BasePipelineConfig:
    @property
    def duration(self) -> float:
        raise NotImplementedError

    @property
    def step(self) -> float:
        raise NotImplementedError

    @property
    def latency(self) -> float:
        raise NotImplementedError

    @property
    def sample_rate(self) -> int:
        raise NotImplementedError


class PipelineConfig(BasePipelineConfig):
    def __init__(
        self,
        segmentation: Optional[m.SegmentationModel] = None,
        embedding: Optional[m.EmbeddingModel] = None,
        duration: Optional[float] = None,
        step: float = 0.5,
        latency: Optional[Union[float, Literal["max", "min"]]] = None,
        tau_active: float = 0.6,
        rho_update: float = 0.3,
        delta_new: float = 1,
        gamma: float = 3,
        beta: float = 10,
        max_speakers: int = 20,
        device: Optional[torch.device] = None,
        **kwargs,
    ):
        # Default segmentation model is pyannote/segmentation
        self.segmentation = segmentation
        if self.segmentation is None:
            self.segmentation = m.SegmentationModel.from_pyannote("pyannote/segmentation")

        # Default duration is the one given by the segmentation model
        self._duration = duration
        if self._duration is None:
            self._duration = self.segmentation.get_duration()

        # Expected sample rate is given by the segmentation model
        self._sample_rate = self.segmentation.get_sample_rate()

        # Default embedding model is pyannote/embedding
        self.embedding = embedding
        if self.embedding is None:
            self.embedding = m.EmbeddingModel.from_pyannote("pyannote/embedding")

        # Latency defaults to the step duration
        self._step = step
        self._latency = latency
        if self._latency is None or self._latency == "min":
            self._latency = self._step
        elif self._latency == "max":
            self._latency = self._duration

        self.tau_active = tau_active
        self.rho_update = rho_update
        self.delta_new = delta_new
        self.gamma = gamma
        self.beta = beta
        self.max_speakers = max_speakers

        self.device = device
        if self.device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    @staticmethod
    def from_namespace(args: Any) -> 'PipelineConfig':
        return PipelineConfig(
            segmentation=getattr(args, "segmentation", None),
            embedding=getattr(args, "embedding", None),
            duration=getattr(args, "duration", None),
            step=args.step,
            latency=args.latency,
            tau_active=args.tau,
            rho_update=args.rho,
            delta_new=args.delta,
            gamma=args.gamma,
            beta=args.beta,
            max_speakers=args.max_speakers,
            device=args.device,
        )

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def step(self) -> float:
        return self._step

    @property
    def latency(self) -> float:
        return self._latency

    @property
    def sample_rate(self) -> int:
        return self._sample_rate


class BasePipeline:
    @property
    def config(self) -> BasePipelineConfig:
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def __call__(
        self,
        waveforms: Sequence[SlidingWindowFeature]
    ) -> Sequence[Tuple[Annotation, SlidingWindowFeature]]:
        raise NotImplementedError


class OnlineSpeakerDiarization(BasePipeline):
    def __init__(self, config: Optional[PipelineConfig] = None):
        self._config = PipelineConfig() if config is None else config

        msg = f"Latency should be in the range [{self._config.step}, {self._config.duration}]"
        assert self._config.step <= self._config.latency <= self._config.duration, msg

        self.segmentation = SpeakerSegmentation(self._config.segmentation, self._config.device)
        self.embedding = OverlapAwareSpeakerEmbedding(
            self._config.embedding, self._config.gamma, self._config.beta, norm=1, device=self._config.device
        )
        self.pred_aggregation = DelayedAggregation(
            self._config.step,
            self._config.latency,
            strategy="hamming",
            cropping_mode="loose",
        )
        self.audio_aggregation = DelayedAggregation(
            self._config.step,
            self._config.latency,
            strategy="first",
            cropping_mode="center",
        )
        self.binarize = Binarize(self._config.tau_active)

        # Internal state, handle with care
        self.clustering = None
        self.chunk_buffer, self.pred_buffer = [], []
        self.reset()

    @property
    def config(self) -> PipelineConfig:
        return self._config

    def reset(self):
        self.clustering = OnlineSpeakerClustering(
            self.config.tau_active,
            self.config.rho_update,
            self.config.delta_new,
            "cosine",
            self.config.max_speakers,
        )
        self.chunk_buffer, self.pred_buffer = [], []

    def __call__(
        self,
        waveforms: Sequence[SlidingWindowFeature]
    ) -> Sequence[Tuple[Annotation, SlidingWindowFeature]]:
        batch_size = len(waveforms)
        msg = "Pipeline expected at least 1 input"
        assert batch_size >= 1, msg

        # Create batch from chunk sequence, shape (batch, samples, channels)
        batch = torch.stack([torch.from_numpy(w.data) for w in waveforms])

        expected_num_samples = int(np.rint(self.config.duration * self.config.sample_rate))
        msg = f"Expected {expected_num_samples} samples per chunk, but got {batch.shape[1]}"
        assert batch.shape[1] == expected_num_samples, msg

        # Extract segmentation and embeddings
        segmentations = self.segmentation(batch)  # shape (batch, frames, speakers)
        embeddings = self.embedding(batch, segmentations)  # shape (batch, speakers, emb_dim)

        seg_resolution = waveforms[0].extent.duration / segmentations.shape[1]

        outputs = []
        for wav, seg, emb in zip(waveforms, segmentations, embeddings):
            # Add timestamps to segmentation
            sw = SlidingWindow(
                start=wav.extent.start,
                duration=seg_resolution,
                step=seg_resolution,
            )
            seg = SlidingWindowFeature(seg.cpu().numpy(), sw)

            # Update clustering state and permute segmentation
            permuted_seg = self.clustering(seg, emb)

            # Update sliding buffer
            self.chunk_buffer.append(wav)
            self.pred_buffer.append(permuted_seg)

            # Aggregate buffer outputs for this time step
            agg_waveform = self.audio_aggregation(self.chunk_buffer)
            agg_prediction = self.pred_aggregation(self.pred_buffer)
            outputs.append((self.binarize(agg_prediction), agg_waveform))

            # Make place for new chunks in buffer if required
            if len(self.chunk_buffer) == self.pred_aggregation.num_overlapping_windows:
                self.chunk_buffer = self.chunk_buffer[1:]
                self.pred_buffer = self.pred_buffer[1:]

        return outputs
