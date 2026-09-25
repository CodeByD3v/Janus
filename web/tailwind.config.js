/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        janus: {
          bg: '#f6f7f9',
          card: '#ffffff',
          border: '#e5e7eb',
          text: '#111827',
          muted: '#6b7280',
          green: '#16a34a',
          greenBg: '#dcfce7',
          blue: '#2563eb',
          blueBg: '#dbeafe',
          red: '#dc2626',
          redBg: '#fee2e2',
          gray: '#9ca3af',
          grayBg: '#f3f4f6',
        }
      },
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Cascadia Code', 'monospace'],
      },
    },
  },
  plugins: [],
}
