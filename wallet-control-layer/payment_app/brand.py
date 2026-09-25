"""The LEASH marks, as inline SVG.

From the brand board (LEASH Logo-System v1.1, decided 2026-09-04, vectors rebuilt 2026-09-24):
one drawing, three cuts. The master signet from 48 px; the small cut with a heavier stroke and
a wider loop from 24 to 47 px; the 16-px cut with no loop under 24 px. The grip — the short
stroke at the top with the round end — is always orange (``#E28E34``), everywhere but in the
mono version. The wordmark is the signet standing in for the L, then EASH in Inter SemiBold
as paths, tracked 8 % of the cap height, the E starting 22 units plus half a stroke after the
signet. ``by one`` is the endorsement, typographic until the issuer's brand releases the
one-mark as a vector.

Everything is drawn in the board's own units — cap height 100, baseline at y = 0, up is
negative — and scaled by the ``width``/``height`` attributes, so a mark is the same drawing at
every size and no bitmap is kept in the repository.
"""

from __future__ import annotations

from dataclasses import dataclass

MIDNIGHT = "#15192C"
ORANGE = "#E28E34"


@dataclass(frozen=True, slots=True)
class Cut:
    stroke: float
    body: str  # the loop, the stem and the foot
    grip: str  # the orange stroke
    dot: tuple[float, float, float]  # the round end of the grip: cx, cy, r
    box: tuple[float, float, float, float]  # viewBox, with room for the stroke


CUTS: dict[str, Cut] = {
    "master": Cut(
        stroke=17.9,
        body=(
            "M 62 0 L 25.1 0 C 11.3 0 0 -11.3 0 -25.1 L 0 -88 C 0 -120 -10 -148 -36 -152 "
            "C -66 -156 -74 -116 -46 -107 C -28 -101 8 -110 34 -140"
        ),
        grip="M 34 -140 L 50.5 -161.2",
        dot=(50.5, -161.2, 8.95),
        box=(-80, -174, 154, 186),
    ),
    "small": Cut(
        stroke=24.16,
        body=(
            "M 62 0 L 33.83 0 C 15.15 0 0 -15.15 0 -33.83 L 0 -88 C 0 -131.2 -13.5 -169 "
            "-48.6 -174.4 C -89.1 -179.8 -99.9 -125.8 -62.1 -113.65 "
            "C -37.8 -105.55 10.8 -117.7 45.9 -158.2"
        ),
        grip="M 45.9 -158.2 L 68.16 -186.8",
        dot=(68.16, -186.8, 12.08),
        box=(-108, -202, 192, 216),
    ),
    "16": Cut(
        stroke=28.64,
        body="M 62 0 L 40.1 0 C 17.95 0 0 -17.95 0 -40.1 L 0 -100",
        grip="M 0 -100 L 0 -142.96",
        dot=(0, -142.96, 14.32),
        box=(-16, -160, 94, 176),
    ),
}

#: EASH, Inter SemiBold at cap height 100, and where each letter starts.
WORDMARK_GLYPHS: tuple[tuple[float, str], ...] = (
    (82.88, "M10.07 0V-100H75.03V-84.9H27.99V-57.92H71.54V-43.02H27.99V-15.1H75.3V0Z"),
    (
        174.1,
        "M3.36 0 38.26 -100H61.01L96.71 0H77.05L68.52 -24.77H31.14L22.95 0ZM35.91 -39.19H63.62"
        "L59.26 -51.95Q57.05 -58.86 54.66 -67.25Q52.28 -75.64 49.46 -86.04Q46.78 -75.57 44.5 "
        "-67.08Q42.21 -58.59 40.13 -51.95Z",
    ),
    (
        280.69,
        "M45.17 1.54Q27.72 1.54 17.35 -6.51Q6.98 -14.56 6.38 -29.4H24.03Q24.63 -21.48 30.6 -1"
        "7.62Q36.58 -13.76 45.03 -13.76Q53.89 -13.76 59.5 -17.75Q65.1 -21.74 65.1 -28.19Q65.1"
        " -34.03 60.13 -37.01Q55.17 -40 47.05 -42.08L35.7 -45.03Q23.15 -48.26 16.14 -54.77Q9."
        "13 -61.28 9.13 -71.88Q9.13 -80.74 13.89 -87.35Q18.66 -93.96 26.91 -97.65Q35.17 -101."
        "34 45.64 -101.34Q56.31 -101.34 64.33 -97.65Q72.35 -93.96 76.91 -87.45Q81.48 -80.94 8"
        "1.68 -72.62H64.3Q63.62 -78.99 58.56 -82.55Q53.49 -86.11 45.37 -86.11Q36.91 -86.11 32"
        ".08 -82.38Q27.25 -78.66 27.25 -72.95Q27.25 -68.72 29.83 -66.07Q32.42 -63.42 36.34 -6"
        "1.81Q40.27 -60.2 44.3 -59.19L53.62 -56.78Q61.01 -54.97 67.75 -51.58Q74.5 -48.19 78.7"
        "6 -42.48Q83.02 -36.78 83.02 -28.05Q83.02 -19.26 78.52 -12.58Q74.03 -5.91 65.57 -2.18"
        "Q57.11 1.54 45.17 1.54Z",
    ),
    (378.09, "M10.07 0V-100H27.99V-58.66H74.5V-100H92.42V0H74.5V-43.56H27.99V0Z"),
)
WORDMARK_BOX = (-80, -174, 556, 186)


def cut_for(px: float) -> str:
    """The board's size rule: the master loop closes under 32 px, so the cuts take over."""
    if px >= 48:
        return "master"
    if px >= 24:
        return "small"
    return "16"


def _strokes(cut: Cut, color: str, grip: str) -> str:
    cx, cy, r = cut.dot
    return (
        f'<g fill="none" stroke-linecap="round" stroke-linejoin="round">'
        f'<path stroke="{color}" stroke-width="{cut.stroke}" d="{cut.body}"/>'
        f'<path stroke="{grip}" stroke-width="{cut.stroke}" stroke-linecap="butt" d="{cut.grip}"/>'
        f"</g>"
        f'<circle fill="{grip}" cx="{cx}" cy="{cy}" r="{r}"/>'
    )


def signet(
    px: float,
    *,
    cut: str | None = None,
    color: str = MIDNIGHT,
    grip: str | None = None,
    cls: str = "",
) -> str:
    """The signet at `px` pixels high, in the cut the board prescribes for that size.

    `grip=None` keeps it orange; pass `color` for the mono version, or white for reverse."""
    chosen = CUTS[cut or cut_for(px)]
    x, y, w, h = chosen.box
    width = round(px * w / h, 2)
    return (
        f'<svg class="{cls}" width="{width}" height="{px}" viewBox="{x} {y} {w} {h}" '
        f'aria-hidden="true" focusable="false">{_strokes(chosen, color, grip or ORANGE)}</svg>'
    )


def wordmark(px: float, *, color: str = MIDNIGHT, grip: str | None = None, cls: str = "") -> str:
    """LEASH: the master signet as the L, then EASH as paths. `px` is the height."""
    x, y, w, h = WORDMARK_BOX
    width = round(px * w / h, 2)
    letters = "".join(f'<path transform="translate({dx} 0)" d="{d}"/>' for dx, d in WORDMARK_GLYPHS)
    return (
        f'<svg class="{cls}" width="{width}" height="{px}" viewBox="{x} {y} {w} {h}" '
        f'role="img" aria-label="LEASH">{_strokes(CUTS["master"], color, grip or ORANGE)}'
        f'<g fill="{color}">{letters}</g></svg>'
    )


__all__ = ["CUTS", "MIDNIGHT", "ORANGE", "cut_for", "signet", "wordmark"]
