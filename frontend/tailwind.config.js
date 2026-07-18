/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Surfaces
        bg:        'rgb(var(--bg)         / <alpha-value>)',
        surface:   'rgb(var(--surface)    / <alpha-value>)',
        'surface-2': 'rgb(var(--surface-2) / <alpha-value>)',
        'bubble-bg': 'rgb(var(--bubble-bg) / <alpha-value>)',

        // Text
        ink:       'rgb(var(--text)        / <alpha-value>)',
        'ink-muted':  'rgb(var(--text-muted)  / <alpha-value>)',
        'ink-faint':  'rgb(var(--text-faint)  / <alpha-value>)',

        // Borders
        edge:      'rgb(var(--border)       / <alpha-value>)',
        'edge-strong': 'rgb(var(--border-strong) / <alpha-value>)',

        // Accent
        accent:    'rgb(var(--accent)       / <alpha-value>)',
        'accent-hover': 'rgb(var(--accent-hover) / <alpha-value>)',
        'accent-soft':  'rgb(var(--accent-soft)  / <alpha-value>)',
        'accent-text':  'rgb(var(--accent-text)  / <alpha-value>)',

        // Status
        success:   'rgb(var(--success)      / <alpha-value>)',
        'success-bg': 'rgb(var(--success-bg) / <alpha-value>)',
        warning:   'rgb(var(--warning)      / <alpha-value>)',
        'warning-bg': 'rgb(var(--warning-bg) / <alpha-value>)',
        danger:    'rgb(var(--danger)       / <alpha-value>)',
        'danger-bg':  'rgb(var(--danger-bg)  / <alpha-value>)',

        // Canvas
        'canvas-bg':   'rgb(var(--canvas-bg)   / <alpha-value>)',
        'canvas-grid': 'rgb(var(--canvas-grid) / <alpha-value>)',
      },
    },
  },
  plugins: [],
}