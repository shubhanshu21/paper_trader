/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        kiteOrange: "#ff5722",
        kiteBlue: "#4184f3",
        kiteGreen: "#4caf50",
        kiteRed: "#ff5722",
      }
    },
  },
  plugins: [],
}
