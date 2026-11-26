#!/usr/bin/env python3
"""ctypes bindings for the SDRplay API 3.15: only what Slot17 uses.

Written from SDRplay's API specification 3.15 and checked against the API's own header files by
scripts/verify_sdrplay.py: every structure's size and every field's offset, compiled for 64-bit ARM
(the Raspberry Pi) and 64-bit x86 Linux, and every bound function's arguments. A wrong offset here would
not fail loudly; it would tune the wrong frequency or set the wrong gain. So:

  * API_VERSION is the one version these layouts were checked against; the receiver refuses any other.
  * check_domains() reads the parameters the API hands out before anything is written, and refuses to go
    on when a field lies outside what the specification allows: on a real Pi that is the last check that
    these structures and the installed library agree.

The API itself is not here and never will be: SDRplay's licence grants no redistribution. setup.sh runs
SDRplay's own installer, which shows SDRplay's licence to the person installing it.

The library is loaded by load(), not at import, so this module can be imported, and its layouts tested,
on a machine without the API.
"""
import ctypes
import os
import struct
import sys
from ctypes import (CFUNCTYPE, POINTER, Structure, Union, c_char, c_char_p, c_double, c_float, c_int, c_ubyte,
                    c_uint, c_ushort, c_void_p)

API_VERSION = 3.15
MAX_DEVICES = 16                 # SDRPLAY_MAX_DEVICES

# Hardware versions (sdrplay_api.h); the receivers Slot17 drives are the RSP1B and the RSP1A.
HW_NAMES = {1: "RSP1", 255: "RSP1A", 2: "RSP2", 3: "RSPduo", 4: "RSPdx", 6: "RSP1B", 7: "RSPdxR2"}
RSP1A_ID, RSP1B_ID = 255, 6

# sdrplay_api_ErrT
SUCCESS = 0
SERVICE_NOT_RESPONDING = 14
ERRORS = {0: "Success", 1: "Fail", 2: "InvalidParam", 3: "OutOfRange", 4: "GainUpdateError", 5: "RfUpdateError",
          6: "FsUpdateError", 7: "HwError", 8: "AliasingError", 9: "AlreadyInitialised", 10: "NotInitialised",
          11: "NotEnabled", 12: "HwVerError", 13: "OutOfMemError", 14: "ServiceNotResponding", 15: "StartPending",
          16: "StopPending", 17: "InvalidMode", 18: "FailedVerification1", 19: "FailedVerification2",
          20: "FailedVerification3", 21: "FailedVerification4", 22: "FailedVerification5",
          23: "FailedVerification6", 24: "InvalidServiceVersion"}

# Enumerations used (C enums are int on every platform the API supports).
ISOCH, BULK = 0, 1                                        # sdrplay_api_TransferModeT
BW = (200, 300, 600, 1536, 5000, 6000, 7000, 8000)        # sdrplay_api_Bw_MHzT, in kHz
BW_5_000 = 5000
IF_TYPES = (-1, 0, 450, 1620, 2048)                       # sdrplay_api_If_kHzT
IF_ZERO = 0
LO_MODES = (0, 1, 2, 3, 4)                                # sdrplay_api_LoModeT
LO_AUTO = 1
MIN_GR = (0, 20)                                          # sdrplay_api_MinGainReductionT
NORMAL_MIN_GR = 20
AGC_MODES = (0, 1, 2, 3, 4)                               # sdrplay_api_AgcControlT
AGC_DISABLE = 0
TUNER_A = 1                                               # sdrplay_api_TunerSelectT
UPDATE_TUNER_GR = 0x00008000                              # sdrplay_api_ReasonForUpdateT
UPDATE_CTRL_OVERLOAD_ACK = 0x04000000
UPDATE_EXT1_NONE = 0
EVENT_GAIN_CHANGE, EVENT_POWER_OVERLOAD, EVENT_DEVICE_REMOVED, EVENT_RSPDUO_MODE, EVENT_DEVICE_FAILURE = 0, 1, 2, 3, 4
OVERLOAD_DETECTED, OVERLOAD_CORRECTED = 0, 1
LNA_STATES_60_420MHZ = 10                                 # RSPIA_NUM_LNA_STATES; RSP1B: same table (spec 5)
GR_MIN, GR_MAX = 20, 59                                   # IF gain reduction, dB (MAX_BB_GR)


# --- sdrplay_api_dev.h, sdrplay_api_rsp1a.h, sdrplay_api_rsp2.h, sdrplay_api_rspDuo.h, sdrplay_api_rspDx.h ---
class FsFreqT(Structure):
    _fields_ = [("fsHz", c_double), ("syncUpdate", c_ubyte), ("reCal", c_ubyte)]


class SyncUpdateT(Structure):
    _fields_ = [("sampleNum", c_uint), ("period", c_uint)]


class ResetFlagsT(Structure):
    _fields_ = [("resetGainUpdate", c_ubyte), ("resetRfUpdate", c_ubyte), ("resetFsUpdate", c_ubyte)]


class Rsp1aParamsT(Structure):
    _fields_ = [("rfNotchEnable", c_ubyte), ("rfDabNotchEnable", c_ubyte)]


class Rsp2ParamsT(Structure):
    _fields_ = [("extRefOutputEn", c_ubyte)]


class RspDuoParamsT(Structure):
    _fields_ = [("extRefOutputEn", c_int)]


class RspDxParamsT(Structure):
    _fields_ = [("hdrEnable", c_ubyte), ("biasTEnable", c_ubyte), ("antennaSel", c_int),
                ("rfNotchEnable", c_ubyte), ("rfDabNotchEnable", c_ubyte)]


class DevParamsT(Structure):
    _fields_ = [("ppm", c_double), ("fsFreq", FsFreqT), ("syncUpdate", SyncUpdateT), ("resetFlags", ResetFlagsT),
                ("mode", c_int), ("samplesPerPkt", c_uint), ("rsp1aParams", Rsp1aParamsT),
                ("rsp2Params", Rsp2ParamsT), ("rspDuoParams", RspDuoParamsT), ("rspDxParams", RspDxParamsT)]


# --- sdrplay_api_tuner.h ---
class GainValuesT(Structure):
    _fields_ = [("curr", c_float), ("max", c_float), ("min", c_float)]


class GainT(Structure):
    _fields_ = [("gRdB", c_int), ("LNAstate", c_ubyte), ("syncUpdate", c_ubyte), ("minGr", c_int),
                ("gainVals", GainValuesT)]


class RfFreqT(Structure):
    _fields_ = [("rfHz", c_double), ("syncUpdate", c_ubyte)]


class DcOffsetTunerT(Structure):
    _fields_ = [("dcCal", c_ubyte), ("speedUp", c_ubyte), ("trackTime", c_int), ("refreshRateTime", c_int)]


class TunerParamsT(Structure):
    _fields_ = [("bwType", c_int), ("ifType", c_int), ("loMode", c_int), ("gain", GainT), ("rfFreq", RfFreqT),
                ("dcOffsetTuner", DcOffsetTunerT)]


# --- sdrplay_api_control.h ---
class DcOffsetT(Structure):
    _fields_ = [("DCenable", c_ubyte), ("IQenable", c_ubyte)]


class DecimationT(Structure):
    _fields_ = [("enable", c_ubyte), ("decimationFactor", c_ubyte), ("wideBandSignal", c_ubyte)]


class AgcT(Structure):
    _fields_ = [("enable", c_int), ("setPoint_dBfs", c_int), ("attack_ms", c_ushort), ("decay_ms", c_ushort),
                ("decay_delay_ms", c_ushort), ("decay_threshold_dB", c_ushort), ("syncUpdate", c_int)]


class ControlParamsT(Structure):
    _fields_ = [("dcOffset", DcOffsetT), ("decimation", DecimationT), ("agc", AgcT), ("adsbMode", c_int)]


# --- sdrplay_api_rx_channel.h and the per-model tuner parameters ---
class Rsp1aTunerParamsT(Structure):
    _fields_ = [("biasTEnable", c_ubyte)]


class Rsp2TunerParamsT(Structure):
    _fields_ = [("biasTEnable", c_ubyte), ("amPortSel", c_int), ("antennaSel", c_int), ("rfNotchEnable", c_ubyte)]


class RspDuoResetSlaveFlagsT(Structure):
    _fields_ = [("resetGainUpdate", c_ubyte), ("resetRfUpdate", c_ubyte)]


class RspDuoTunerParamsT(Structure):
    _fields_ = [("biasTEnable", c_ubyte), ("tuner1AmPortSel", c_int), ("tuner1AmNotchEnable", c_ubyte),
                ("rfNotchEnable", c_ubyte), ("rfDabNotchEnable", c_ubyte), ("resetSlaveFlags", RspDuoResetSlaveFlagsT)]


class RspDxTunerParamsT(Structure):
    _fields_ = [("hdrBw", c_int)]


class RxChannelParamsT(Structure):
    _fields_ = [("tunerParams", TunerParamsT), ("ctrlParams", ControlParamsT),
                ("rsp1aTunerParams", Rsp1aTunerParamsT), ("rsp2TunerParams", Rsp2TunerParamsT),
                ("rspDuoTunerParams", RspDuoTunerParamsT), ("rspDxTunerParams", RspDxTunerParamsT)]


# --- sdrplay_api.h ---
class DeviceT(Structure):
    _fields_ = [("SerNo", c_char * 64), ("hwVer", c_ubyte), ("tuner", c_int), ("rspDuoMode", c_int),
                ("valid", c_ubyte), ("rspDuoSampleFreq", c_double), ("dev", c_void_p)]


class DeviceParamsT(Structure):
    _fields_ = [("devParams", POINTER(DevParamsT)), ("rxChannelA", POINTER(RxChannelParamsT)),
                ("rxChannelB", POINTER(RxChannelParamsT))]


class ErrorInfoT(Structure):
    _fields_ = [("file", c_char * 256), ("function", c_char * 256), ("line", c_int), ("message", c_char * 1024)]


# --- sdrplay_api_callback.h ---
class GainCbParamT(Structure):
    _fields_ = [("gRdB", c_uint), ("lnaGRdB", c_uint), ("currGain", c_double)]


class PowerOverloadCbParamT(Structure):
    _fields_ = [("powerOverloadChangeType", c_int)]


class RspDuoModeCbParamT(Structure):
    _fields_ = [("modeChangeType", c_int)]


class EventParamsT(Union):
    _fields_ = [("gainParams", GainCbParamT), ("powerOverloadParams", PowerOverloadCbParamT),
                ("rspDuoModeParams", RspDuoModeCbParamT)]


class StreamCbParamsT(Structure):
    _fields_ = [("firstSampleNum", c_uint), ("grChanged", c_int), ("rfChanged", c_int), ("fsChanged", c_int),
                ("numSamples", c_uint)]


# void (*)(short *xi, short *xq, sdrplay_api_StreamCbParamsT *params, unsigned int numSamples,
#          unsigned int reset, void *cbContext)
# The three pointers arrive as plain addresses (c_void_p: the same C calling convention as the typed pointers):
# the API calls this thousands of times a second, and building ctypes pointer objects for each call was most of
# its cost on the Pi. STREAM_PARAMS reads the parameter structure (its layout is in the checked STRUCTS).
StreamCallback = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p, c_uint, c_uint, c_void_p)
STREAM_PARAMS = struct.Struct("=Iiii")       # firstSampleNum, grChanged, rfChanged, fsChanged (offsets 0, 4, 8, 12)
# void (*)(sdrplay_api_EventT eventId, sdrplay_api_TunerSelectT tuner, sdrplay_api_EventParamsT *params,
#          void *cbContext)
EventCallback = CFUNCTYPE(None, c_int, c_int, POINTER(EventParamsT), c_void_p)


class CallbackFnsT(Structure):
    _fields_ = [("StreamACbFn", StreamCallback), ("StreamBCbFn", StreamCallback), ("EventCbFn", EventCallback)]


# Every structure, in the order the layout check compares them with the C headers.
STRUCTS = {
    "sdrplay_api_FsFreqT": FsFreqT, "sdrplay_api_SyncUpdateT": SyncUpdateT, "sdrplay_api_ResetFlagsT": ResetFlagsT,
    "sdrplay_api_Rsp1aParamsT": Rsp1aParamsT, "sdrplay_api_Rsp2ParamsT": Rsp2ParamsT,
    "sdrplay_api_RspDuoParamsT": RspDuoParamsT, "sdrplay_api_RspDxParamsT": RspDxParamsT,
    "sdrplay_api_DevParamsT": DevParamsT, "sdrplay_api_GainValuesT": GainValuesT, "sdrplay_api_GainT": GainT,
    "sdrplay_api_RfFreqT": RfFreqT, "sdrplay_api_DcOffsetTunerT": DcOffsetTunerT,
    "sdrplay_api_TunerParamsT": TunerParamsT, "sdrplay_api_DcOffsetT": DcOffsetT,
    "sdrplay_api_DecimationT": DecimationT, "sdrplay_api_AgcT": AgcT, "sdrplay_api_ControlParamsT": ControlParamsT,
    "sdrplay_api_Rsp1aTunerParamsT": Rsp1aTunerParamsT, "sdrplay_api_Rsp2TunerParamsT": Rsp2TunerParamsT,
    "sdrplay_api_RspDuo_ResetSlaveFlagsT": RspDuoResetSlaveFlagsT,
    "sdrplay_api_RspDuoTunerParamsT": RspDuoTunerParamsT, "sdrplay_api_RspDxTunerParamsT": RspDxTunerParamsT,
    "sdrplay_api_RxChannelParamsT": RxChannelParamsT, "sdrplay_api_DeviceT": DeviceT,
    "sdrplay_api_DeviceParamsT": DeviceParamsT, "sdrplay_api_ErrorInfoT": ErrorInfoT,
    "sdrplay_api_GainCbParamT": GainCbParamT, "sdrplay_api_PowerOverloadCbParamT": PowerOverloadCbParamT,
    "sdrplay_api_RspDuoModeCbParamT": RspDuoModeCbParamT, "sdrplay_api_EventParamsT": EventParamsT,
    "sdrplay_api_StreamCbParamsT": StreamCbParamsT, "sdrplay_api_CallbackFnsT": CallbackFnsT,
}

# name: (restype, argtypes), exactly as the prototypes in sdrplay_api.h.
FUNCTIONS = {
    "sdrplay_api_Open": (c_int, []),
    "sdrplay_api_Close": (c_int, []),
    "sdrplay_api_ApiVersion": (c_int, [POINTER(c_float)]),
    "sdrplay_api_LockDeviceApi": (c_int, []),
    "sdrplay_api_UnlockDeviceApi": (c_int, []),
    "sdrplay_api_GetDevices": (c_int, [POINTER(DeviceT), POINTER(c_uint), c_uint]),
    "sdrplay_api_SelectDevice": (c_int, [POINTER(DeviceT)]),
    "sdrplay_api_ReleaseDevice": (c_int, [POINTER(DeviceT)]),
    "sdrplay_api_GetErrorString": (c_char_p, [c_int]),
    "sdrplay_api_GetLastError": (POINTER(ErrorInfoT), [POINTER(DeviceT)]),
    "sdrplay_api_GetDeviceParams": (c_int, [c_void_p, POINTER(POINTER(DeviceParamsT))]),
    "sdrplay_api_Init": (c_int, [c_void_p, POINTER(CallbackFnsT), c_void_p]),
    "sdrplay_api_Uninit": (c_int, [c_void_p]),
    "sdrplay_api_Update": (c_int, [c_void_p, c_int, c_int, c_int]),
}


class SdrplayError(RuntimeError):
    def __init__(self, what, code, detail=""):
        self.code = code
        super().__init__(f"{what} failed: {ERRORS.get(code, code)}" + (f" ({detail})" if detail else ""))


def library_path():
    """Where SDRplay's installer puts the library; SLOT17_SDRPLAY_LIB overrides (tests, other installs)."""
    override = os.environ.get("SLOT17_SDRPLAY_LIB")
    if override:
        return override
    if sys.platform == "win32":
        return r"C:\Program Files\SDRplay\API\x64\sdrplay_api.dll"
    return "libsdrplay_api.so.3"             # /usr/local/lib, found through ldconfig after the installer


def load(path=None):
    """The API with every function Slot17 uses bound to its exact prototype."""
    lib = ctypes.CDLL(path or library_path())
    for name, (restype, argtypes) in FUNCTIONS.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
    return lib


def error_string(lib, code):
    try:
        text = lib.sdrplay_api_GetErrorString(code)
        return text.decode("ascii", "replace") if text else ERRORS.get(code, str(code))
    except Exception:                        # noqa: BLE001 - a message, never a second failure
        return ERRORS.get(code, str(code))


def check(lib, code, what):
    if code != SUCCESS:
        raise SdrplayError(what, code, error_string(lib, code))


def check_domains(dp):
    """Problems with the parameters the API handed out, read before anything is written to them.

    Every field read here has a short list of legal values in the specification. A structure that is
    laid out differently from the installed library reads neighbouring bytes instead, and those fail
    these checks almost surely. An empty list means the layouts agree.
    """
    problems = []
    if not dp.devParams or not dp.rxChannelA:
        return ["the API returned no device or channel parameters"]
    dev, ch = dp.devParams.contents, dp.rxChannelA.contents
    t, c = ch.tunerParams, ch.ctrlParams

    def need(ok, what):
        if not ok:
            problems.append(what)

    need(2e6 <= dev.fsFreq.fsHz <= 10.66e6, f"fsHz {dev.fsFreq.fsHz!r}")
    need(dev.mode in (ISOCH, BULK), f"transfer mode {dev.mode}")
    need(-1000 <= dev.ppm <= 1000, f"ppm {dev.ppm!r}")
    need(dev.rsp1aParams.rfNotchEnable in (0, 1) and dev.rsp1aParams.rfDabNotchEnable in (0, 1), "RSP1A notch flags")
    need(t.bwType in BW, f"bwType {t.bwType}")
    need(t.ifType in IF_TYPES, f"ifType {t.ifType}")
    need(t.loMode in LO_MODES, f"loMode {t.loMode}")
    need(0 <= t.gain.gRdB <= 59, f"gRdB {t.gain.gRdB}")
    need(t.gain.LNAstate < 28, f"LNAstate {t.gain.LNAstate}")
    need(t.gain.minGr in MIN_GR, f"minGr {t.gain.minGr}")
    need(1e3 <= t.rfFreq.rfHz <= 2e9, f"rfHz {t.rfFreq.rfHz!r}")
    need(c.dcOffset.DCenable in (0, 1) and c.dcOffset.IQenable in (0, 1), "DC/IQ correction flags")
    need(c.decimation.enable in (0, 1) and c.decimation.decimationFactor in (1, 2, 4, 8, 16, 32),
         f"decimation {c.decimation.enable}/{c.decimation.decimationFactor}")
    need(c.agc.enable in AGC_MODES, f"AGC mode {c.agc.enable}")
    need(-120 <= c.agc.setPoint_dBfs <= 0, f"AGC set point {c.agc.setPoint_dBfs}")
    need(ch.rsp1aTunerParams.biasTEnable in (0, 1), "bias-T flag")
    return problems
