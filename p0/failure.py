"""Shared failure categories; none authorizes automatic receiver reopening."""
STREAM_STALL = 75
RECEIVER_UNAVAILABLE = 76
ANALYSIS_FAILURE = 77
RUNTIME_FAILURE = 1


class StreamStall(RuntimeError):
    pass


class AnalysisFailure(RuntimeError):
    pass


def exit_code(exc, receiver_opened=True):
    if isinstance(exc, StreamStall):
        return STREAM_STALL
    if isinstance(exc, AnalysisFailure):
        return ANALYSIS_FAILURE
    if not receiver_opened:
        return RECEIVER_UNAVAILABLE
    return RUNTIME_FAILURE


def outcome(code):
    return {0: 'clean', STREAM_STALL: 'stall',
            RECEIVER_UNAVAILABLE: 'receiver unavailable',
            ANALYSIS_FAILURE: 'analysis failure'}.get(code, 'crash')
