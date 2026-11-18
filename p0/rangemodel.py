#!/usr/bin/env python3
"""Uncalibrated link-budget scenarios for research, not measured distance.

The piecewise model joins free-space loss to the asymptotic two-ray law at
4*pi*h_tx*h_rx/lambda (36.1 m for 1.5 m heights at 383 MHz). It is one possible
propagation scenario: road clutter, diffraction, cabin loss and antenna patterns
are not measured here. NF=8 dB, net receive loss=-10 dB and transmitter powers
15/34.8/40 dBm are assumptions. An empirical channel floor is not calibrated dBm.
The returned three scenarios are neither confidence bounds nor a range guarantee.
Live alerts deliberately omit these metre values. See docs/design/Range-and-Sensitivity.md.
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

C = 299_792_458.0
DEFAULT_F_HZ = 383.0e6
CHANNEL_HZ = 25_000.0

# Assumed whole-system noise figure, unmeasured; not the receiver data-sheet NF.
NOISE_FIGURE_DB = 8.0
THERMAL_DBM_PER_HZ = -174.0

# Geometry: both antennas roughly roof height above the road.
H_TX_M = 1.5
H_RX_M = 1.5

# Transmit power bracket, dBm. Low end = a power-controlled radio sitting next to
# its base station; high end = a 10 W vehicle set working hard. The TYPICAL value
# is what the point estimate uses.
PTX_LOW_DBM = 15.0        # lowest listed power-control level; scenario, not measured
PTX_TYPICAL_DBM = 34.8    # 3 W  - ETSI class 3, the common vehicle/portable set
PTX_HIGH_DBM = 40.0       # 10 W - ETSI class 2

# Antennas. The TX end is a proper vehicle whip; ours is whatever is on the Pi.
G_TX_DBI = 2.0
G_RX_DBI = 0.0
# Assumed combined receive-side loss. A balanced dipole does not need a car ground plane.
RX_LOSS_DB = -10.0

# If a calibration drive is ever done (see docs/design/Range-and-Sensitivity.md), put the
# fitted values here and set CALIBRATED = True. Until then the model is pure
# physics and says so.
CALIBRATED = False
CAL_REF_SNR_DB = None
CAL_REF_M = None
CAL_EXPONENT = None


def noise_floor_dbm(nf_db=NOISE_FIGURE_DB, bw_hz=CHANNEL_HZ):
    return THERMAL_DBM_PER_HZ + 10.0 * math.log10(bw_hz) + nf_db


def fspl_db(d_m, f_hz=DEFAULT_F_HZ):
    d_m = max(float(d_m), 1e-3)
    return 20.0 * math.log10(4.0 * math.pi * d_m * f_hz / C)


def breakpoint_m(f_hz=DEFAULT_F_HZ, h_tx=H_TX_M, h_rx=H_RX_M):
    """Two-ray critical distance: beyond it the slope doubles to 40 dB/decade."""
    lam = C / f_hz
    return 4.0 * math.pi * h_tx * h_rx / lam


def path_loss_db(d_m, f_hz=DEFAULT_F_HZ, h_tx=H_TX_M, h_rx=H_RX_M):
    """Two-ray ground reflection: free space to the breakpoint, then 40 dB/dec."""
    dc = breakpoint_m(f_hz, h_tx, h_rx)
    if d_m <= dc:
        return fspl_db(d_m, f_hz)
    return fspl_db(dc, f_hz) + 40.0 * math.log10(d_m / dc)


def distance_for_path_loss(pl_db, f_hz=DEFAULT_F_HZ, h_tx=H_TX_M, h_rx=H_RX_M):
    """Invert path_loss_db()."""
    dc = breakpoint_m(f_hz, h_tx, h_rx)
    pl_c = fspl_db(dc, f_hz)
    if pl_db <= pl_c:
        # still inside the free-space region
        return dc * 10.0 ** ((pl_db - pl_c) / 20.0)
    return dc * 10.0 ** ((pl_db - pl_c) / 40.0)


def range_m(snr_db, nf_db=NOISE_FIGURE_DB, f_hz=DEFAULT_F_HZ,
            rx_loss_db=RX_LOSS_DB, g_rx_dbi=G_RX_DBI, g_tx_dbi=G_TX_DBI):
    """Return central, low-power and high-power scenario distances only."""
    if CALIBRATED:
        d = CAL_REF_M * 10.0 ** (-(snr_db - CAL_REF_SNR_DB) / (10.0 * CAL_EXPONENT))
        return d, d / 2.0, d * 2.0

    floor = noise_floor_dbm(nf_db)
    p_rx = floor + float(snr_db)
    out = []
    for ptx in (PTX_LOW_DBM, PTX_TYPICAL_DBM, PTX_HIGH_DBM):
        pl = ptx + g_tx_dbi + g_rx_dbi + rx_loss_db - p_rx
        out.append(distance_for_path_loss(pl, f_hz))
    low, typ, high = out
    return typ, low, high


def describe(snr_db, **kw):
    typ, lo, hi = range_m(snr_db, **kw)
    return f"Uncalibrated scenarios: {lo:.0f} / {typ:.0f} / {hi:.0f} m; actual distance unknown"


# --------------------------------------------------------------------------- #

def selftest():
    # Analytic consistency checks, not assertions about a vehicle's distance.
    assert abs(breakpoint_m() - 4 * math.pi * H_TX_M * H_RX_M * DEFAULT_F_HZ / C) < 1e-9
    for d in (1, 20, 36.1, 100, 1000, 5000):
        assert math.isclose(distance_for_path_loss(path_loss_db(d)), d, rel_tol=1e-9)
    for d in (100, 1000):
        asymptotic = 40 * math.log10(d) - 20 * math.log10(H_TX_M * H_RX_M)
        assert math.isclose(path_loss_db(d), asymptotic, abs_tol=1e-9)
    assert math.isclose(PTX_HIGH_DBM - PTX_LOW_DBM, 25)
    print(f"PASS: analytic two-ray crossover {breakpoint_m():.3f} m and inverse; field range unmeasured")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--snr", type=float, help="SNR in dB above the channel floor")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    if args.snr is None:
        ap.print_help()
        return 0
    print(describe(args.snr))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
