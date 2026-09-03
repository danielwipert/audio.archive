from __future__ import annotations

from .ableton import PcmWavService
from .config import AppConfig
from .policy import WAV24_VARIANT
from .tooling import CommandRunner


class Wav24Service(PcmWavService):
    """A 24-bit integer PCM WAV made from the verified source master.

    PROJECT_SPEC section 9.3 permits this only as an explicit request, because integer
    quantization is a choice the user makes rather than a default the application may
    apply. It is a compatibility copy for tools and players that dislike 32-bit float,
    not a replacement for the Ableton intermediate: it carries no more source
    information, and the source master remains canonical.

    Nothing else about the decode changes. The source sample rate and mono/stereo
    layout are preserved, no filter, resampling, normalization or dither is applied,
    and long-form output is segmented and verified under the same policy.
    """

    def __init__(self, config: AppConfig, runner: CommandRunner):
        super().__init__(config, runner, WAV24_VARIANT)
