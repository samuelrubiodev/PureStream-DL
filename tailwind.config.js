/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  // Escanea el HTML (y el JS inline que contiene) para generar solo las
  // clases realmente usadas -> CSS mínimo de producción.
  content: ['./templates/index.html'],
  theme: {
    extend: {
      colors: { brand: { 500: '#6366f1', 600: '#4f46e5', 700: '#4338ca' } },
    },
  },
  plugins: [],
};