// Sentinel's ivory-ledger palette, taken from the Sentinel source
// (princedaniel1197/KPCL, branch sentinel-v2, tailwind.config.ts), which is what
// https://kpcl.vercel.app serves. These are the ONLY colours in CRPMS, and
// colour signals state: red Bad, amber Uncertain, green Good.
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        paper: '#F5F1E8',
        panel: '#FBF9F3',
        ink: '#2A2418',
        muted: '#7A7260',
        faint: '#A39B87',
        gold: '#C9A84C',
        hairline: '#CBB97F',
        wash: '#EFE9DA',
        success: '#5B6E3A',
        danger: '#8C3B2E',
        warning: '#A9762B',
        info: '#5C6B7A',
      },
      fontFamily: {
        display: ['var(--font-display)', 'Georgia', 'serif'],
        sans: ['var(--font-sans)', 'system-ui', 'sans-serif'],
      },
      borderWidth: { hairline: '0.5px', master: '1.5px' },
    },
  },
  plugins: [],
}
