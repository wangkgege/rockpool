"""
Simulation of an analog audio filtering front-end

Defines :py:class:`.AFESim` module.

See Also:
    For example usage of the :py:class:`.AFESim` Module, see :ref:`/devices/analog-frontend-example.ipynb`
"""

# - Rockpool imports
import os
from rockpool.nn.modules.module import Module
from rockpool.nn.modules.native.filter_bank import ButterFilter
from rockpool.timeseries import TSEvent, TSContinuous
from rockpool.parameters import Parameter, State, SimulationParameter, ParameterBase

# - Other imports
import numpy as np
from scipy.signal import butter, lfilter
from scipy import signal, fftpack
import enum

from typing import Union, Tuple

from logging import debug, info

info = print
debug = print

from rockpool.typehints import P_int, P_float, P_bool, P_ndarray

from .afe_spike_generation import _encode_spikes

# Define exports
__all__ = ["AFESim"]


class AFESim(Module):
    """
    A :py:class:`.Module` that simulates analog hardware for preprocessing audio and converting into spike features.

    This module simulates the Xylo audio front-end stage. This is a signal-to-event core that consists of a number of band-pass filters, followed by rectifying event production
    simulating a spiking LIF neuron. The event rate in each channel is roughly correlated to the energy in each filter band.

    Notes:
        - The AFE contains frequency tripling internally. For accurate simulation, the sampling frequency must be at least 6 times higher than the highest frequency component in the filtering chain. This would be the centre frequency of the highest filter, plus half the BW of that signal. To prevent signal aliasing, you should apply a low-pass filter to restrict the bandwidth of the input, to ensure you don't exceed this target highest frequency.

        - Input to the module is in Volts. Input amplitude should be scaled to a maximum of 112mV RMS.

        - By default, the module simulates HW mismatch in the analog encoding block. This is controlled on instantiation with the ``add_noise``, ``add_offset`` and ``add_mismatch`` arguments on initialisation and the corresponding simulation parameters.

        - Mismatch can be re-sampled by calling the :py:meth:`.generate_mismatch` method

    See Also:
        For example usage of the :py:class:`.AFESim` Module, see :ref:`/devices/analog-frontend-example.ipynb`
    """

    def __init__(
        self,
        fs: int,  # this should be the same as the sampling rate of the audio fed to AFESim. Otherwise, the frequencies are proportionally shifted.
        raster_period: float = 0.01,  # this is the period to which the generated spikes are rastered
        max_spike_per_raster_period: int = 15,  # maximum number of spikes to be forwarded to SNN in Xylo within a raster period
        add_noise: bool = True,
        add_offset: bool = True,
        add_mismatch: bool = True,
        seed: int = np.random.randint(2**32 - 1),
        num_workers: int = 1,
        *args,
        **kwargs,
    ):
        """
        Parameters
        ----------
        fs: int
            NOTE:
                (i)     AFE contains frequency tripling in LNA and also in microphone. So the maximum representable frequency is ``fs/6``.
                (ii)    If this frequency is different than the sampling frequency of the audio, the frequencies are proportionally shifted and extracted spikes features will be wrong.
        raster_period (float): this is the period to which the spikes are rastered and counted. Default 10ms.
        max_spike_per_raster_period (int): maximum number of spikes in each rastering period. This is equal to 15 spikes in Xylo-A2.
        add_noise: bool
            Enables / disables the simulated noise generated be the AFE. Default: ``True``, include noise
        add_offset: bool
            If ``True`` (default), add mismatch offset to each filter
        add_mismatch: bool
            If ``True`` (default), add simualted mismatch to each filter
        seed: int
            The AFE is subject to mismatch, this can be seeded by providing an integer seed. Default: random seed. Provide ``None`` to prevent seeding.
        num_workers: int
            Number of cpu units used to speed up filter computation. Default: 1.
        """

        ###### Check shape argument and Initialize the superclass ######
        shape = (1, 16)
        super().__init__(shape=shape, spiking_output=True, *args, **kwargs)

        ## Provide pRNG seed
        self.seed: P_int = SimulationParameter(seed, init_func=lambda _: None)
        if self.seed is not None:
            np.random.seed(self.seed)

        ##### Set the rastering parameters for the produced spike #####
        self.raster_period = raster_period
        self.max_spike_per_raster_period = max_spike_per_raster_period

        ###### Power supply features and maximum voltage of the chip ######
        # Maximum bipolar amplitude that can be supported by the the chip without being clipped
        # NOTE: in the chip, we have a voltage supply in the range [0, 1.1] volts where the refernce voltages of LNA and filters is
        # shifted to the middle value of 0.55 volts.
        # So, in terms of signal processing, we can always assume that the input signal is a bipolar one in the range [-0.55, 0.55] with clipping
        # if the amplitude goes beyond this range
        self.VCC: P_float = SimulationParameter(1.1)  # in volts
        self.INPUT_MAX_AMPLITUDE: P_float = SimulationParameter(
            self.VCC / 2.0
        )  # in volts

        ###### microphone fetaures ######
        # NOTE: microphone has severe THD for sound level above 120 dBL SPL, which is 20 Pa pressure.
        # Microphone has a sensitivity of around -40 dB in units of Volt/Pa which will be 10 mV/Pa.
        # For maximum 20 Pa pressure, this yields a maximum amplitude of 200 mV.
        self.MIC_DISTORTION: P_float = SimulationParameter(0.01)
        self.INPUT_MIC_MAX_AMPLITUDE: P_float = SimulationParameter(200e-3)  # 200mV
        """ float: Maximum amplitude of sound that can be produced with microphone without having sever THD (above 1%)"""

        ###### from microphone to LNA ######
        self.MAX_INPUT_OFFSET: P_float = SimulationParameter(0.0)  # from microphone
        """ float: Maxmimum input offset from microphone (Default 0.) """

        # Corner frequency of the AC coupling between the microphone and LNA
        self.F_CORNER_HIGHPASS: P_float = SimulationParameter(20)
        """ float: High pass corner frequency due to AC Coupling from BPF to FWR in Hz. (Default 20 Hz)"""

        ###### LNA features ######
        # the linear regime of LNA with a given nonlinearity threshold
        # NOTE: in Xylo-A2 the microphone has a very low sensitivity and the maximum voltage it can produce is 200 mV where beyond that
        # THD is very large.
        # For this reson, the LNA distortion would be almost negiligible compared with that of the microphone.
        self.LNA_DISTORTION: P_float = SimulationParameter(0.01)
        self.INPUT_LNA_MAX_AMPLITUDE: P_float = SimulationParameter(
            self.INPUT_MAX_AMPLITUDE * 0.8
        )
        """ float: LNA Distortion parameter when the amplitude goes beyond its linear regime. Default 0.01 """

        # LNA gain
        # possible gains in LNA: it is possible to adjust LNA by to have an amplification of order 2 or 4
        class LNA_GAIN(enum.Enum):
            G0dB = 0.0  # in dB
            G6dB = 6.0  # in dB
            G12dB = 12.0  # in dB

        self.lna_gain_db: P_float = SimulationParameter(LNA_GAIN.G0dB.value)  # in dB
        """ float: Low-noise amplifer gain in dB (Default 0.) """

        self.MAX_LNA_OFFSET: P_float = SimulationParameter(5.0e-3)  # +/-5mV random
        """ float: Maxmimum low-noise amplifier offset in mV (Default 5mV) """

        ###### filterbank features ######
        # - Parameters for BPF
        self.Q: P_int = SimulationParameter(4)
        """ int: Quality parameter for band-pass filters """

        self.Qs: np.ndarray = self.Q * np.ones(self.size_out)
        """ np.ndarray: Q factors for each filter """

        # - sampling frequency of the incoming audio signal
        self.Fs: P_float = SimulationParameter(fs)
        """ float: Sample frequency of input data """

        # - nominal/design values of the filters center frequencies and bandwidths
        self.design_fcs: P_ndarray = SimulationParameter(
            np.asarray(
                [
                    40,
                    54,
                    77,
                    137,
                    203,
                    290,
                    428,
                    674,
                    1177,
                    1700,
                    2226,
                    3418,
                    5154,
                    7884,
                    11630,
                    16940,
                ]
            )
        )
        """ np.ndarray: Centre frequency of each band-pass filter in Hz """

        self.fcs: np.ndarray = self.design_fcs
        """ np.ndarray: Actual centre frequencies of each band-pass filter in Hz """

        self.bws: np.ndarray = self.fcs / self.Qs
        """ np.ndarray: Actual bandwidth for each filter """

        # - Check the filters w.r.t the sampling frequency
        if self.Fs < (6 * np.max(self.design_fcs)):
            raise ValueError(
                f"""Sampling frequency ({self.Fs}) must be at least 6 times the highest BPF centre freq. (i.e. >{6 * np.max(self.design_fcs)} Hz)
                The main reason is that the microphone produces THD (third-order distortion) which may fallback into the wrong frequency
                if the sampling frequency is not large enough.
                """
            )

        # - nominal/design Bandwidths of the filters
        self.design_bws: P_ndarray = SimulationParameter(self.design_fcs / self.Q)
        """ np.ndarray: Bandwidths of each filter in Hz """

        # - order of the Butterworth BPF used in the filterbank
        self.ORDER_BPF: P_int = SimulationParameter(2)
        """ int: Band-pass filter order (Default 2)"""

        self.MAX_BPF_OFFSET: P_float = SimulationParameter(5.0e-3)  # +/-5mV random
        """ float: Maxmum band-pass filter offset in mV (Default 5mV)"""

        self.BPF_FC_SHIFT: P_float = SimulationParameter(
            -5e-2
        )  # 5 for +5%    -5 for -5%  NOTE: 16 channels center freq shift in the same direction
        """ float: Centre frequency band-pass filter shift in % (Default -5%) """

        self.Q_MIS_MATCH: P_float = SimulationParameter(10e-2)  # +/-10% random
        """ float: Mismatch in Q in % (Default 10%) """

        self.FC_MIS_MATCH: P_float = SimulationParameter(5e-2)  # +/-5% random
        """ float: Mismatch in centre freq. in % (Default 5%)"""

        ###### spike generation features  ######
        # capacitor in LIF circuit used for integration and spike generation
        self.C_IAF: P_float = SimulationParameter(5e-12)  # 5 pF
        """ float: Integrator Capacitance for IAF (Default 5e-12)"""

        # V2I module gain: how the input voltage is converted to current for integration and spike generation
        # this is set to be I=2e-8 for a max voltage of V=60mv
        self.V2I_GIAN: P_float = SimulationParameter(0.333e-6)

        self.LEAKAGE: P_float = SimulationParameter(1e-9)
        """ float: Leakage conductance for LIF neuron producing the spikes. Default: 1.0e-9 : 1.0 nA for a cpacitor at volatage 1.0V """

        self.DIGITAL_COUNTER: P_int = SimulationParameter(4)
        """ int: Digital counter factor to reduce output spikes by. Default 4 (by a factor 4) """

        # Threshold for spike generation using IAF (essentially LIF) neuron
        self.THR_UP: P_float = SimulationParameter(
            0.5
        )  # 0.1-0.9 V to be in the linear regime of system.
        """ float: Threshold for delta modulation in V (0.1--0.9) (Default 0.5V)"""

        ##### Other settings for simulation #####
        self.add_noise: P_bool = SimulationParameter(add_noise)
        """ bool: Flag indicating that noise should be simulated during operation. Default `True` """

        self.add_offset: P_bool = SimulationParameter(add_offset)
        """ bool: Flag indicating that offset should be simulated during operation. Default `True` """

        self.add_mismatch: P_bool = SimulationParameter(add_mismatch)
        """ bool: Flag indicating that mismatch in the parameters should be simulated during operation. Default `True` """

        self.num_workers: P_int = SimulationParameter(num_workers)
        """ int: number of independent CPU units used for simulating the filters in the filterbank. Default 1"""

        ### Macro definitions related to noise ###
        self.VRMS_SQHZ_LNA: P_float = SimulationParameter(70e-9)
        self.F_KNEE_LNA: P_float = SimulationParameter(70e3)
        self.F_ALPHA_LNA: P_float = SimulationParameter(1)

        self.VRMS_SQHZ_BPF: P_float = SimulationParameter(1e-9)
        self.F_KNEE_BPF: P_float = SimulationParameter(100e3)
        self.F_ALPHA_BPF: P_float = SimulationParameter(1)

        self.VRMS_SQHZ_FWR: P_float = SimulationParameter(700e-9)
        self.F_KNEE_FWR: P_float = SimulationParameter(158)
        self.F_ALPHA_FWR: P_float = SimulationParameter(1)

        ### Initialise mismatch parameters
        self.input_offset: P_float = SimulationParameter(0.0)
        """ float: Mismatch offset in the signal comming from microphone -- typically 0 due to AC coupling """

        self.lna_offset: P_float = SimulationParameter(0.0)
        """ float: Mismatch offset in low-noise amplifier """

        self.bpf_offset: P_ndarray = SimulationParameter(np.zeros(self.size_out))
        """ float: Mismatch offset in band-pass filters """

        self.Q_mismatch: P_ndarray = SimulationParameter(np.zeros(self.size_out))
        """ float: Mismatch in Q over band-pass filters """

        self.fc_mismatch: P_ndarray = SimulationParameter(np.zeros(self.size_out))
        """ float: Mismatch in centre frequency for band-pass filters """

        self.bpf_fc_shift: P_float = SimulationParameter(0.0)
        """ float: Common shift in center frequencies due to temperature, etc. """

        # - Event generation state
        self.lif_state: P_ndarray = State(np.zeros(self.size_out))
        """ (np.ndarray) Internal state of the LIF neurons used to generate events """

        # Initialize chip parameters, generate mismatch if required
        self.generate_mismatch()

    def generate_mismatch(self):
        """
        This function generates mismatch for the parameters of AFESim, based on analog non-idealities.
        It may be called:
            (i)     once for all inputs simulated if we are interested in a single chip for all simulations.
            (ii)    once for each input if we would like some sort of augmentation w.r.t. chip non-idealities.
        """
        self.input_offset = (
            self.MAX_INPUT_OFFSET * (2 * np.random.rand(1).item() - 1.0)
            if self.add_offset
            else 0.0
        )

        self.lna_offset = (
            self.MAX_LNA_OFFSET * (2 * np.random.rand(1).item() - 1.0)
            if self.add_offset
            else 0.0
        )

        self.bpf_offset = (
            self.MAX_BPF_OFFSET * (2 * np.random.rand(self.size_out) - 1.0)
            if self.add_offset
            else np.zeros(self.size_out)
        )

        self.Q_mismatch = (
            self.Q_MIS_MATCH * (2 * np.random.rand(self.size_out) - 1.0)
            if self.add_mismatch
            else np.zeros(self.size_out)
        )

        self.fc_mismatch = (
            self.FC_MIS_MATCH * (2 * np.random.rand(self.size_out) - 1.0)
            if self.add_mismatch
            else np.zeros(self.size_out)
        )

        self.bpf_fc_shift = (
            self.BPF_FC_SHIFT * (2 * np.random.rand(1).item() - 1.0)
            if self.add_mismatch
            else 0.0
        )

        # Shift center frequencies and bandwidths based on mismatch parameters
        self.fcs = np.asarray(
            [
                freq * (1.0 + mismatch)
                for (freq, mismatch) in zip(self.design_fcs, self.fc_mismatch)
            ]
        )

        # Shift the center frequencies together by mismatch factor
        self.fcs = np.asarray([freq * (1.0 + self.bpf_fc_shift) for freq in self.fcs])

        # Produce Q for the filters
        self.Qs = np.asarray(
            [self.Q * (1.0 + mismatch) for mismatch in self.Q_mismatch]
        )

        # Produce bandwidths
        # self.bws = np.asarray([freq / Q for (freq, Q) in zip(self.fcs, self.Qs)])
        self.bws = self.fcs / self.Qs

        # - Re-generate the filterbank module
        self._butter_filterbank = ButterFilter(
            frequency=self.fcs,
            bandwidth=self.bws,
            fs=self.Fs,
            order=self.ORDER_BPF,
            num_workers=self.num_workers,
            use_lowpass=False,
        )

        # - High-pass filter parameters (for AC coupling between microphone and LNA)
        self._HP_filt = self._butter_highpass(self.F_CORNER_HIGHPASS, self.Fs, order=1)
        """ High-pass filter on input """

        # - Reset internal neuron state
        self.lif_state: P_ndarray = State(np.zeros(self.size_out))
        """ (np.ndarray) Internal state of the LIF neurons used to generate events """

    ##### Utility functions: filters #####
    def _butter_bandpass(
        self, lowcut: float, highcut: float, fs: float, order: int = 2
    ) -> Tuple[float, float]:
        """
        Build a Butterworth bandpass filter from specification

        Args:
            lowcut (float): Low-cut frequency in Hz
            highcut (float): High-cut frequency in Hz
            fs (float): Sampling frequecy in Hz
            order (int): Order of the filter

        Returns: (float, float): b, a
            Parameters for the bandpass filter
        """
        nyq = 0.5 * fs
        low = lowcut / nyq
        high = highcut / nyq
        b, a = butter(order, [low, high], btype="band", output="ba")
        return b, a

    def _butter_bandpass_filter(
        self, data: np.ndarray, lowcut: float, highcut: float, fs: float, order: int = 2
    ) -> np.ndarray:
        """
        Filter data with a bandpass Butterworth filter, according to specifications

        Args:
            data (np.ndarray): Input data with shape ``(T, N)``
            lowcut (float): Low-cut frequency in Hz
            highcut (float): High-cut frequency in Hz
            fs (float): Sampling frequency in Hz
            order (int): Order of the filter

        Returns: np.ndarray: Filtered data with shape ``(T, N)``
        """
        b, a = self._butter_bandpass(lowcut, highcut, fs, order=order)
        y = lfilter(b, a, data)
        return y

    def _butter_highpass(
        self, cutoff: float, fs: float, order: int = 1
    ) -> Tuple[float, float]:
        """
        Build a Butterworth high-pass filter from specifications

        Args:
            cutoff (float): High-pass cutoff frequency in Hz
            fs (float): Sampling rate in Hz
            order (int): Order of the filter

        Returns: (float, float): b, a
            Parameters for the high-pass filter
        """
        nyq = 0.5 * fs
        normal_cutoff = cutoff / nyq
        b, a = butter(order, normal_cutoff, btype="high", analog=False)
        return b, a

    def _butter_highpass_filter(
        self, data: np.ndarray, cutoff: float, fs: float, order: int = 1
    ) -> np.ndarray:
        """
        Filter some data with a Butterworth high-pass filter from specifications

        Args:
            data (np.ndarray): Array of input data to filter, with shape ``(T, N)``
            cutoff (float): Cutoff frequency of the high-pass filter, in Hz
            fs (float): Sampling frequency of ``data``, in Hz
            order (int): Order of the Butterwoth filter

        Returns: np.ndarray: Filtered output data with shape ``(T, N)``
        """
        b, a = self._butter_highpass(cutoff, fs, order=order)
        y = signal.filtfilt(b, a, data)
        return y

    #### Utility functions: noise ####
    def _generateNoise(
        self,
        T,
        Fs: float = 16e3,
        VRMS_SQHZ: float = 1e-6,
        F_KNEE: float = 1e3,
        F_ALPHA: float = 1.4,
    ) -> np.ndarray:
        """
        Generate band-limited noise, for use in simulating the AFE architecture

        Args:
            x (np.ndarray): Input signal defining desired shape of noise ``(T,)``
            Fs (float): Sampling frequency in Hz
            VRMS_SQHZ (float):
            F_KNEE (float):
            F_ALPHA (float):

        Returns: np.ndarray: Generated noise with shape ``(T,)``
        """

        def one_over_f(f: np.ndarray, knee: float, alpha: float) -> np.ndarray:
            d = np.ones_like(f)
            f = np.clip(f, 1e-12, np.inf)
            d[f < knee] = np.abs(((knee / f[f < knee]) ** (alpha)))
            d[0] = 1
            return d

        W_NOISE_SIGMA = VRMS_SQHZ * np.sqrt(Fs / 2)  # Noise in the bandwidth 0 - Fs/2

        wn = np.random.normal(0, W_NOISE_SIGMA, T)
        s = fftpack.rfft(wn)
        f = fftpack.rfftfreq(len(s)) * Fs
        ff = s * one_over_f(f, F_KNEE, F_ALPHA)
        x_t = fftpack.irfft(ff)

        return x_t

    #### Utility functions: spike generation #####
    def _sampling_spikes(self, spikes: np.ndarray, count: int) -> np.ndarray:
        """
        Down-sample events in a signal, by passing one in every ``N`` events

        Args:
            spikes (np.ndarray): Raster ``(T, N)`` of events
            count (int): Number of events to ignore before passing one event

        Returns: np.ndarray: Raster ``(T, N)`` of down-sampled events
        """

        return (np.cumsum(spikes, axis=0) % count * spikes) == (count - 1)

    #### Utility functions: modelling the distortion #####
    def _MIC_evolve(
        self, sig_in: np.ndarray, v_corner: float, THD_level, max_output: float
    ):
        """this function incorporates the effect of third-order distortion in the input audio signal.

        Args:
            sig_in (np.ndarray): 1-dim input signal.
            v_corner (float): voltage level at which THD is equal to THD_level.
            THD_level (float, optional): level of third-order distortion.
            max_output (float): maximum signal amplitude in the whole chip.
        """
        # we use the simple formula sin(3 th) = 3 sin(th) - 4 sin^3(th) for simulating THD
        distortion = (
            THD_level
            * v_corner
            * (3 * sig_in / v_corner - 4 * (sig_in / v_corner) ** 3)
        )

        sig_out = sig_in + distortion

        sig_out[sig_out > max_output] = max_output
        sig_out[sig_out < -max_output] = -max_output

        return sig_out

    def _LNA_evolve(
        self,
        sig_in: np.ndarray,
        v_corner: float,
        lna_distortion_level: float,
        max_output: float,
    ):
        """this function takes the nonlinearity due to LNA into account.

        Args:
            sig_in (np.ndarray): input signal received from the microphone.
            v_corner (float): the maximum linear range of the LNA.
            lna_distortion_level (float): the nonlinear distortion level added by LNA.
            max_output (float): maximum signal amplitude in the whole chip.
        """

        # input signal is amplified by LNA
        lna_gain = 2 ** (self.lna_gain_db / 6.0)
        sig_out = lna_gain * (sig_in + self.lna_offset)

        # for a symmetric LNA, the main contribution of distortion is due to 3rd order nonlinearity
        lna_out = v_corner * (
            sig_out / v_corner - lna_distortion_level * (sig_out / v_corner) ** 3
        )

        # truncate the amplitude when the LNA goes into the saturation regime
        lna_out[lna_out > max_output] = max_output
        lna_out[lna_out < -max_output] = -max_output

        return lna_out

    #### Utility functions: state representation #####
    @property
    def dt(self) -> float:
        """
        Simulation time-step in seconds

        Returns:
            float: Simulation time-step
        """
        return 1 / self.Fs

    def _wrap_recorded_state(self, state_dict: dict, t_start: float = 0.0) -> dict:
        args = {"dt": self.dt, "t_start": t_start}

        return {
            "LNA_out": TSContinuous.from_clocked(
                state_dict["LNA_out"], name="LNA", **args
            ),
            "BPF": TSContinuous.from_clocked(state_dict["BPF"], name="BPF", **args),
            "rect": TSContinuous.from_clocked(state_dict["rect"], name="Rect", **args),
            "spks_out": TSEvent.from_raster(
                state_dict["spks_out"],
                name="Spikes",
                num_channels=self.size_out,
                **args,
            ),
        }

    def evolve(
        self,
        input: np.ndarray,
        record: bool = False,
        *args,
        **kwargs,
    ) -> Tuple[np.ndarray, dict, dict]:
        """Evolve AFESim and return the generated spikes

        Args:
            input (np.ndarray, optional): input audio signal.
            record (bool, optional): Record the internal state of AFESim during evolution. Defaults to ``False``.

        Raises:
            ValueError: If the input data is not 1D

        Returns:
            np.ndarray: Output events (T, 16), where each bin contains the count of events in that bin.
            dict: Internal state of the module at the end of evolution
            dict: Recording dictionary if requested, otherwise an empty dictionary
        """

        # - Make sure input is 1D
        if np.ndim(input) > 1:
            raise ValueError("the input signal should be 1-dim.")

        #### Microphone model ####
        mic_out = self._MIC_evolve(
            sig_in=input,
            v_corner=self.INPUT_MIC_MAX_AMPLITUDE,
            THD_level=self.MIC_DISTORTION,
            max_output=self.INPUT_MAX_AMPLITUDE,
        )

        if self.add_offset:
            mic_out += self.input_offset

        ####   LNA - Gain  ####
        lna_out = self._LNA_evolve(
            sig_in=mic_out,
            v_corner=self.INPUT_LNA_MAX_AMPLITUDE,
            lna_distortion_level=self.LNA_DISTORTION,
            max_output=self.INPUT_MAX_AMPLITUDE,
        )

        if self.add_offset:
            lna_out += self.lna_offset

        ####  Add Noise #####
        if self.add_noise:
            noise = self._generateNoise(
                input.shape[0],
                self.Fs,
                self.VRMS_SQHZ_LNA,
                self.F_KNEE_LNA,
                self.F_ALPHA_LNA,
            )
            lna_out += noise

        #### filterbank processing ####
        # - Expand lna_output dimensions and add offset
        filter_in = np.tile(np.atleast_2d(lna_out).T, (1, self.size_out))

        if self.add_offset:
            filter_in += self.bpf_offset

        # - Perform the filtering
        filtered, _, _ = self._butter_filterbank(filter_in)

        # add noise
        if self.add_noise:
            for i in range(self.size_out):
                filtered[:, i] += self._generateNoise(
                    input.shape[0],
                    self.Fs,
                    self.VRMS_SQHZ_BPF,
                    self.F_KNEE_BPF,
                    self.F_ALPHA_BPF,
                )

        # - HP filt, additional noise, rectify
        # NOTE: HP filter should be essentially at the input where the AC coupling between microphone and LNA lies.
        # However, we can also do it at the output together with rectifier
        rectified = np.zeros_like(filtered)
        for i in range(self.size_out):
            rectified[:, i] = abs(
                signal.filtfilt(*self._HP_filt, filtered[:, i])
                # rectified[:, i]
                + self._generateNoise(
                    input.shape[0],
                    self.Fs,
                    self.VRMS_SQHZ_FWR,
                    self.F_KNEE_FWR,
                    self.F_ALPHA_FWR,
                )
            )

        # Encoding to spike by integrating the FWR output for positive going(UP)
        spikes, new_state = _encode_spikes(
            initial_state=self.lif_state,
            dt=self.dt,
            data=rectified,
            v2i_gain=self.V2I_GIAN,
            c_iaf=self.C_IAF,
            leakage=self.LEAKAGE,
            thr_up=self.THR_UP,
            vcc=self.VCC,
        )

        # - Keep a record of the LIF neuron states
        self.lif_state = new_state

        if self.DIGITAL_COUNTER > 1:
            spikes = self._sampling_spikes(spikes, self.DIGITAL_COUNTER)

        recording = (
            {
                "LNA_out": lna_out,
                "BPF": filtered,
                "rect": rectified,
                "spks_out": spikes,
            }
            if record
            else {}
        )

        return spikes, self.state(), recording

    def raster(self, spikes: np.ndarray):
        """
        Rasterise the produced spikes within the rastering period

        Args:
            spikes (np.ndarray): input spikes of dimension `T x F` where `F`: number of filters.
        """
        # convert spikes into numpy: this is due to having jax.numpy array when jax is active
        spikes = np.asarray(spikes)

        # number of clocks within a rastering period
        num_clk_per_period = self.raster_period / self.dt

        # faster method
        if num_clk_per_period == int(num_clk_per_period):
            # sum of spikes during several clocks
            spike_sum = np.cumsum(spikes, axis=0)[:: int(num_clk_per_period), :]

            # number of spikes colected in rastering periods
            spike_sum[1:, :] -= spike_sum[:-1, :]

            # truncate the number of spikes
            spike_sum[
                spike_sum > self.max_spike_per_raster_period
            ] = self.max_spike_per_raster_period

            return spike_sum

        # use rockpool rastering: slower
        spike_sum = TSEvent.from_raster(spikes, dt=self.dt).raster(
            dt=self.raster_period, add_events=True
        )
        spike_sum[
            spike_sum > self.max_spike_per_raster_period
        ] = self.max_spike_per_raster_period

        return spike_sum
