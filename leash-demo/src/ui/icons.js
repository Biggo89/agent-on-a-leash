/** Inline SVG. No icon font, no network. */

/* LEASH logo system v1.1 (LEASH_Logos_Uebersicht.svg). The signet is the L:
   a leash drawn in one stroke, with the grip as the only orange. Three cuts of
   one drawing, picked by rendered width: master from 48 px, small cut from 24,
   the loopless 16-px cut below that. Paths are copied from the sheet, cap
   height 100, baseline 0. */

const INK = '#15192C';
const GRIP = '#E28E34';
const WARM = '#F7F5F0';

const CUTS = {
  master: {
    box: '-73 -171 145 181', w: 17.9,
    body: 'M62 0H25.1C11.3 0 0-11.3 0-25.1V-88C0-120-10-148-36-152C-66-156-74-116-46-107C-28-101 8-110 34-140',
    grip: 'M34-140 50.5-161.2', end: [50.5, -161.2],
  },
  small: {
    box: '-98 -199 179 212', w: 24.16,
    body: 'M62 0H33.83C15.15 0 0-15.15 0-33.83V-88C0-131.2-13.5-169-48.6-174.4C-89.1-179.8-99.9-125.8-62.1-113.65C-37.8-105.55 10.8-117.7 45.9-158.2',
    grip: 'M45.9-158.2 68.16-186.8', end: [68.16, -186.8],
  },
  px16: {
    box: '-15 -158 92 173', w: 28.64,
    body: 'M62 0H40.1C17.95 0 0-17.95 0-40.1V-100',
    grip: 'M0-100V-142.96', end: [0, -142.96],
  },
};

/** The drawing alone, in the sheet's units. `stroke` is the body colour. */
const signetG = ({ body, grip, end, w }, stroke = INK, gripColor = GRIP) =>
  `<g fill="none" stroke-linecap="round" stroke-linejoin="round">` +
  `<path d="${body}" stroke="${stroke}" stroke-width="${w}"/>` +
  `<path d="${grip}" stroke="${gripColor}" stroke-width="${w}" stroke-linecap="butt"/>` +
  `<circle cx="${end[0]}" cy="${end[1]}" r="${w / 2}" fill="${gripColor}" stroke="none"/></g>`;

/** Signet on its own. `cut`: master | small | px16; `reverse` for dark grounds. */
export const signet = (height = 28, { cut = 'small', reverse = false, cls = '' } = {}) => {
  const c = CUTS[cut];
  const [, , bw, bh] = c.box.split(' ').map(Number);
  return `<svg class="${cls}" height="${height}" width="${Math.round((height * bw / bh) * 10) / 10}" viewBox="${c.box}" aria-hidden="true">${signetG(c, reverse ? WARM : INK)}</svg>`;
};

/* EASH in Inter SemiBold as paths, tracking 8 % of cap height, E at x = 82.88. */
const EASH = '<g fill="#15192C"><path transform="translate(82.88)" d="M10.07 0V-100H75.03V-84.9H27.99V-57.92H71.54V-43.02H27.99V-15.1H75.3V0Z"/><path transform="translate(174.1)" d="M3.36 0 38.26 -100H61.01L96.71 0H77.05L68.52 -24.77H31.14L22.95 0ZM35.91 -39.19H63.62L59.26 -51.95Q57.05 -58.86 54.66 -67.25Q52.28 -75.64 49.46 -86.04Q46.78 -75.57 44.5 -67.08Q42.21 -58.59 40.13 -51.95Z"/><path transform="translate(280.69)" d="M45.17 1.54Q27.72 1.54 17.35 -6.51Q6.98 -14.56 6.38 -29.4H24.03Q24.63 -21.48 30.6 -17.62Q36.58 -13.76 45.03 -13.76Q53.89 -13.76 59.5 -17.75Q65.1 -21.74 65.1 -28.19Q65.1 -34.03 60.13 -37.01Q55.17 -40 47.05 -42.08L35.7 -45.03Q23.15 -48.26 16.14 -54.77Q9.13 -61.28 9.13 -71.88Q9.13 -80.74 13.89 -87.35Q18.66 -93.96 26.91 -97.65Q35.17 -101.34 45.64 -101.34Q56.31 -101.34 64.33 -97.65Q72.35 -93.96 76.91 -87.45Q81.48 -80.94 81.68 -72.62H64.3Q63.62 -78.99 58.56 -82.55Q53.49 -86.11 45.37 -86.11Q36.91 -86.11 32.08 -82.38Q27.25 -78.66 27.25 -72.95Q27.25 -68.72 29.83 -66.07Q32.42 -63.42 36.34 -61.81Q40.27 -60.2 44.3 -59.19L53.62 -56.78Q61.01 -54.97 67.75 -51.58Q74.5 -48.19 78.76 -42.48Q83.02 -36.78 83.02 -28.05Q83.02 -19.26 78.52 -12.58Q74.03 -5.91 65.57 -2.18Q57.11 1.54 45.17 1.54Z"/><path transform="translate(378.09)" d="M10.07 0V-100H27.99V-58.66H74.5V-100H92.42V0H74.5V-43.56H27.99V0Z"/></g>';

/** Master wordmark: signet L + EASH. Minimum 96 px wide. */
export const wordmark = (cls = '') =>
  `<svg class="${cls}" viewBox="-73 -171 545 181" role="img" aria-label="LEASH">${signetG(CUTS.master)}${EASH}</svg>`;

/** Endorsement lockup LEASH | by one, as the sheet's own header uses it. Minimum
    240 px wide. "one" is set in Inter SemiBold until Viseca releases the mark,
    and stays in one's own orange (--brand), not the grip's. */
export const lockup = (cls = '') =>
  `<svg class="${cls}" viewBox="-73 -171 851 187" role="img" aria-label="LEASH by one">${signetG(CUTS.master)}${EASH}` +
  `<path d="M510.51-100V0" stroke="#D9DCE5" stroke-width="2"/>` +
  `<g fill="#62697A"><path transform="translate(545.21)" d="M25.15 0.79Q21.5 0.79 19.21 -0.45Q16.92 -1.68 15.62 -3.33Q14.31 -4.97 13.6 -6.26H12.95V0H5.3V-53.31H13.17V-33.49H13.6Q14.28 -34.7 15.55 -36.37Q16.82 -38.03 19.11 -39.27Q21.4 -40.5 25.15 -40.5Q29.95 -40.5 33.72 -38.07Q37.5 -35.64 39.68 -31.02Q41.86 -26.4 41.86 -19.89Q41.86 -13.45 39.71 -8.8Q37.57 -4.15 33.79 -1.68Q30.02 0.79 25.15 0.79ZM23.4 -5.9Q26.83 -5.9 29.14 -7.76Q31.45 -9.62 32.63 -12.81Q33.81 -15.99 33.81 -19.96Q33.81 -23.9 32.65 -27.03Q31.48 -30.16 29.18 -31.99Q26.87 -33.81 23.4 -33.81Q18.32 -33.81 15.67 -30Q13.02 -26.19 13.02 -19.96Q13.02 -13.7 15.71 -9.8Q18.39 -5.9 23.4 -5.9Z"/><path transform="translate(589.11)" d="M4.76 14.38 6.69 7.98 7.58 8.19Q10.63 9.02 12.75 8Q14.88 6.98 15.99 3.11L16.85 0.18L1.65 -40H10.16L18.07 -17.28Q18.96 -14.63 19.64 -12.06Q20.32 -9.48 20.97 -6.91Q21.65 -9.48 22.36 -12.08Q23.08 -14.67 24.01 -17.28L32.06 -40H40.5L23.11 5.55Q21.29 10.34 18.19 12.79Q15.1 15.24 10.27 15.24Q8.41 15.24 6.96 14.97Q5.51 14.7 4.76 14.38Z"/></g><g fill="#f69f29"><path transform="translate(645.96)" d="M22.29 0.82Q16.46 0.82 12.16 -1.77Q7.87 -4.36 5.53 -9Q3.18 -13.63 3.18 -19.79Q3.18 -26.01 5.53 -30.68Q7.87 -35.35 12.16 -37.92Q16.46 -40.5 22.29 -40.5Q28.12 -40.5 32.42 -37.92Q36.71 -35.35 39.07 -30.68Q41.43 -26.01 41.43 -19.79Q41.43 -13.63 39.07 -9Q36.71 -4.36 32.42 -1.77Q28.12 0.82 22.29 0.82ZM22.29 -6.73Q25.55 -6.73 27.69 -8.48Q29.84 -10.23 30.91 -13.2Q31.99 -16.17 31.99 -19.82Q31.99 -23.51 30.91 -26.48Q29.84 -29.45 27.69 -31.2Q25.55 -32.95 22.29 -32.95Q19.07 -32.95 16.92 -31.2Q14.78 -29.45 13.72 -26.48Q12.67 -23.51 12.67 -19.82Q12.67 -16.17 13.72 -13.2Q14.78 -10.23 16.92 -8.48Q19.07 -6.73 22.29 -6.73Z"/><path transform="translate(690.58)" d="M14.24 -23.43V0H4.94V-40H13.74L13.85 -32.02Q17.42 -40.5 26.23 -40.5Q32.42 -40.5 36.15 -36.57Q39.89 -32.63 39.89 -25.44V0H30.59V-24.01Q30.59 -28.01 28.52 -30.3Q26.44 -32.59 22.79 -32.59Q19.07 -32.59 16.65 -30.2Q14.24 -27.8 14.24 -23.43Z"/><path transform="translate(735.41)" d="M22.61 0.82Q16.57 0.82 12.22 -1.72Q7.87 -4.26 5.53 -8.87Q3.18 -13.49 3.18 -19.75Q3.18 -25.9 5.49 -30.57Q7.8 -35.24 12.04 -37.87Q16.28 -40.5 22 -40.5Q26.87 -40.5 31.04 -38.37Q35.21 -36.24 37.75 -31.79Q40.29 -27.33 40.29 -20.32V-17.39H12.45Q12.63 -12.06 15.44 -9.27Q18.25 -6.48 22.72 -6.48Q25.8 -6.48 28 -7.8Q30.2 -9.12 31.13 -11.66L39.68 -9.91Q38.25 -5.08 33.76 -2.13Q29.27 0.82 22.61 0.82ZM12.49 -23.72H31.23Q30.81 -28.01 28.5 -30.61Q26.19 -33.2 22.04 -33.2Q17.78 -33.2 15.3 -30.47Q12.81 -27.73 12.49 -23.72Z"/></g></svg>`;

const svg = (d, extra = '') =>
  `<svg viewBox="0 0 16 16" fill="none" width="14" height="14" aria-hidden="true" ${extra}>${d}</svg>`;

export const tick = svg('<path d="M3 8.4 6.2 11.5 13 4.6" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>');
export const cross = svg('<path d="M4.4 4.4 11.6 11.6M11.6 4.4 4.4 11.6" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>');
export const bang = svg('<path d="M8 4v4.6M8 11.4v.2" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>');
export const dash = svg('<path d="M4.6 8h6.8" stroke="#8e8e93" stroke-width="2" stroke-linecap="round"/>');

export const play = svg('<path d="M4.5 3.4 12.4 8 4.5 12.6z" fill="currentColor"/>');
export const pause = svg('<path d="M5 3.6h2.2v8.8H5zM8.8 3.6H11v8.8H8.8z" fill="currentColor"/>');
export const step = svg('<path d="M4 3.4 10 8l-6 4.6z" fill="currentColor"/><path d="M11.6 3.4v9.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>');
export const reset = svg('<path d="M3.2 8a4.8 4.8 0 1 0 1.5-3.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M2.6 2.4v3h3" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>');

export const lock = svg('<rect x="3.4" y="7" width="9.2" height="6.2" rx="1.6" stroke="currentColor" stroke-width="1.5"/><path d="M5.6 7V5.4a2.4 2.4 0 0 1 4.8 0V7" stroke="currentColor" stroke-width="1.5"/>');
export const wallet = svg('<rect x="2.2" y="4" width="11.6" height="8.4" rx="1.8" stroke="currentColor" stroke-width="1.5"/><path d="M10.6 8.2h3.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>');
export const clock = svg('<circle cx="8" cy="8" r="5.6" stroke="currentColor" stroke-width="1.5"/><path d="M8 5.2V8l1.9 1.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>');
export const shop = svg('<path d="M2.8 6.4h10.4v6.2a.9.9 0 0 1-.9.9H3.7a.9.9 0 0 1-.9-.9z" stroke="currentColor" stroke-width="1.4"/><path d="M2.4 6.4 3.6 3h8.8l1.2 3.4" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>');
export const tag = svg('<path d="M8.6 2.6H13V7l-5.6 5.6a1.2 1.2 0 0 1-1.7 0L2.6 9.5a1.2 1.2 0 0 1 0-1.7z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><circle cx="10.4" cy="5.2" r=".9" fill="currentColor"/>');
export const shield = svg('<path d="M8 2.2 13 4v4c0 3-2.1 5-5 5.8C5.1 13 3 11 3 8V4z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>');
export const user = svg('<circle cx="8" cy="5.8" r="2.6" stroke="currentColor" stroke-width="1.4"/><path d="M3.4 13.2a4.6 4.6 0 0 1 9.2 0" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>');
export const bolt = svg('<path d="M8.8 1.8 4.2 9h3.2l-.9 5.2L12 7H8.6z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>');
export const doc = svg('<path d="M4 2.4h5l3 3v8.2H4z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M9 2.4v3h3" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>');
export const close = svg('<path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>');
export const chevron = svg('<path d="M6 4l4 4-4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>');

export const bigLeash = `
<svg width="72" height="72" viewBox="0 0 32 32" fill="none" aria-hidden="true">
  <path d="M14.6 7.6C19.4 2.4 24.4 2.8 27 6.8" stroke="#f69f29" stroke-width="2.6" stroke-linecap="round"/>
  <circle cx="10.2" cy="10.4" r="5.1" stroke="#1a1a16" stroke-width="2.6"/>
  <path d="M12.7 15.1 16.9 27.2" stroke="#1a1a16" stroke-width="2.6" stroke-linecap="round"/>
</svg>`;

export const VERDICT_ICON = {
  pass: tick,
  violation: cross,
  concern: bang,
  unknown: bang,
  not_applicable: dash,
};

export const sliders = svg(
  '<path d="M3 5h4M11 5h2M3 11h2M9 11h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
    '<circle cx="9" cy="5" r="1.7" stroke="currentColor" stroke-width="1.5"/>' +
    '<circle cx="7" cy="11" r="1.7" stroke="currentColor" stroke-width="1.5"/>',
);
export const back = svg('<path d="M10 4 6 8l4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>');
export const plus = svg('<path d="M8 4v8M4 8h8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>');
