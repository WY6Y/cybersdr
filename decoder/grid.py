"""
grid.py — Maidenhead grid locator utilities.
"""
import math


def grid_to_latlon(grid: str) -> tuple:
    """
    Convert a 4- or 6-character Maidenhead grid square to (lat, lon) of its center.

    Example:
        grid_to_latlon("EL29") → (29.5, -95.0)
        grid_to_latlon("EL29lp") → (29.604..., -95.083...)
    """
    grid = grid.upper().strip()
    if len(grid) < 4:
        raise ValueError(f"Grid square too short (need ≥4 chars): {grid!r}")

    lon = (ord(grid[0]) - ord("A")) * 20 - 180
    lat = (ord(grid[1]) - ord("A")) * 10 - 90
    lon += (ord(grid[2]) - ord("0")) * 2
    lat += (ord(grid[3]) - ord("0")) * 1

    if len(grid) >= 6:
        # Sub-square step: each letter covers 5 min lon × 2.5 min lat
        lon += (ord(grid[4]) - ord("A")) * (2.0 / 24.0)
        lat += (ord(grid[5]) - ord("A")) * (1.0 / 24.0)
        # Center of sub-square
        lon += 1.0 / 24.0
        lat += 0.5 / 24.0
    else:
        # Center of 4-char square
        lon += 1.0
        lat += 0.5

    return lat, lon


def distance_km(grid1: str, grid2: str) -> float:
    """Great circle distance in kilometres between two grid squares."""
    lat1, lon1 = grid_to_latlon(grid1)
    lat2, lon2 = grid_to_latlon(grid2)

    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 1)


def bearing(grid1: str, grid2: str) -> float:
    """True bearing in degrees from grid1 to grid2 (0–360)."""
    lat1, lon1 = grid_to_latlon(grid1)
    lat2, lon2 = grid_to_latlon(grid2)

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)

    brng = math.degrees(math.atan2(x, y))
    return round((brng + 360) % 360, 1)
