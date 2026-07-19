from typing import Optional

import numpy as np


def penman_monteith_pet(
    temp: np.ndarray,
    spfh: np.ndarray,
    dlwrf: np.ndarray,
    dswrf: np.ndarray,
    pres: np.ndarray,
    ugrd_10m: np.ndarray,
    vgrd_10m: np.ndarray,
    albedo: Optional[float] = 0.23,
    eps_s: Optional[float] = 0.98,
) -> np.ndarray:
    """Hourly FAO-56 Penman-Monteith for 2D AORC grids.

    Parameters
    ----------
    temp
        Air temperature at 2 m, degC.
    spfh
        Specific humidity at 2 m, kg kg-1 (auto-detected if in g kg-1).
    dlwrf
        Downward longwave radiation, W m-2.
    dswrf
        Downward shortwave radiation, W m-2.
    pres
        Surface pressure, Pa.
    ugrd_10m
        Eastward wind at 10 m, m s-1.
    vgrd_10m
        Northward wind at 10 m, m s-1.
    albedo
        Surface albedo (default 0.23 for grass reference crop).
    eps_s
        Surface emissivity (default 0.98 for grass reference crop).

    Returns
    -------
    np.ndarray
        PET, same shape as inputs, mm h-1, non-negative.
    """
    spfh = np.where(spfh > 0.02, spfh / 1000.0, spfh)

    P = pres / 1000.0
    u2 = np.sqrt(ugrd_10m**2 + vgrd_10m**2) * 4.87 / np.log(67.8 * 10 - 5.42)
    gamma = 0.000665 * P

    es = 0.6108 * np.exp((17.27 * temp) / (temp + 237.3))
    ea = (spfh * P) / (0.622 + 0.378 * spfh)
    delta = (4098 * es) / (temp + 237.3) ** 2

    Rs = dswrf * 0.0036
    Rl_down = dlwrf * 0.0036

    sigma = 4.903e-9 / 24.0
    Rl_up = eps_s * sigma * (temp + 273.15) ** 4
    Rn = (1 - albedo) * Rs + (Rl_down - Rl_up)

    G = np.where(Rn > 0, 0.1 * Rn, 0.5 * Rn)
    cd = np.where(Rn > 0, 0.24, 0.96)

    num = 0.408 * delta * (Rn - G) + gamma * (37.0 / (temp + 273.15)) * u2 * (es - ea)
    denom = delta + gamma * (1 + cd * u2)
    return np.maximum(num / denom, 0.0)
